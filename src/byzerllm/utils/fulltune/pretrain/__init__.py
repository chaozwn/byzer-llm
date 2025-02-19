from typing import List, Optional, Tuple,Any,Dict,Callable,Generator
from transformers import AutoTokenizer, AutoModelForCausalLM,BitsAndBytesConfig
import ray
import torch
import deepspeed
import deepspeed.comm as dist
import sentencepiece as spm
import numpy as np
import json
import os
from pyjava.storage import streaming_tar as STar
from pyjava import RayContext
from pyjava.api.mlsql import DataServer
from byzerllm import BlockRow
from ray.air.util.torch_dist import (
    ActorHandle,
    _get_node_and_gpu_ids,
    _init_torch_distributed,
    get_address_and_port,
)
import dataclasses
from ray.train.constants import DEFAULT_NCCL_SOCKET_IFNAME

DEFUALT_CONFIG = '''
{
  "gradient_accumulation_steps": 1,
  "train_micro_batch_size_per_gpu": 1,
  "prescale_gradients": false,
  "zero_allow_untested_optimizer": true,
  "optimizer": {
    "type": "AdamW",
    "params": {
      "lr": 1e-8,
      "eps": 1.0e-8,
      "betas": [
        0.9,
        0.95
      ],
      "weight_decay": 0.1
    }
  },
  "tensorboard": {
    "enabled": true,
    "output_path": "logs/",
    "job_name": "baichuan-7b-pt"
  },
  "zero_optimization": {
    "stage": 2,
    "offload_optimizer": {
         "device": "cpu"
     },
    "contiguous_gradients": true,
    "allgather_bucket_size": 1e8,
    "reduce_bucket_size": 1e8,
    "overlap_comm": true,
    "reduce_scatter": true
  },
  "steps_per_print": 16,
  "gradient_clipping": 1.0,
  "wall_clock_breakdown": true,
  "bf16": {
    "enabled": true
  }
}

'''

@dataclasses.dataclass
class TrainArgs:
    model_path: str = "" 
    tokenizer_path: str = ""
    steps_per_epoch: int = 4096
    is_partition_data: bool = False
    epoches:int = 1
    checkpoint_saving_path: str = "/home/byzerllm/data/checkpoints"
    max_length: int = 4096
    data_dir: str = "/home/byzerllm/data/raw_data"
     

@dataclasses.dataclass
class DeviceID:
    node_id: int
    gpu_ids: List[int]
    rank: int

class DataEngine():
    def __init__(self, data_dir, tokenizer_path, micro_batch_size, max_length,world_size,rank):
        self.MIN_TEXT_LEN = 20
        self.EOS_TOKEN_ID = 2
        self.data_dir = data_dir
        self.sp = spm.SentencePieceProcessor()
        self.sp.Load(tokenizer_path)
        self.micro_batch_size = micro_batch_size
        self.max_length = max_length
        self.data = []
        self.global_input_paths = [self.data_dir + "/" + x
                                   for x in os.listdir(self.data_dir)]
        self.local_input_paths = [x for i, x in
                                  enumerate(self.global_input_paths)
                                  if i % world_size == rank]

    def load_data(self):
        for file_path in self.local_input_paths:
            data = []
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                for line_id, line in enumerate(f):
                    cc = self.sp.EncodeAsIds(line.strip()) + [self.EOS_TOKEN_ID]
                    if len(cc) < self.MIN_TEXT_LEN:
                        cc = []
                    data.extend(cc)
                    if len(data) >= self.micro_batch_size * (self.max_length + 1):
                        index = self.micro_batch_size * (self.max_length + 1)
                        self.data.append(data[:index])
                        data = []
        return

    def get_data(self):
        data = self.data.pop(0)
        seq = np.asarray(data).reshape(self.micro_batch_size, self.max_length + 1)
        data = torch.LongTensor(seq)
        data = data.cuda(non_blocking=True)
        return data

class ParallelConfig:
    """Configuration for the distributed execution.    
    """

    def __init__(
        self,
        num_workers:int,            
        get_model:Callable[[str,Dict],Any],        
        ds_config:Dict[Any,Any],         
        data_refs:List[DataServer] = [],
        train_args = TrainArgs(),            
        backend: str = "nccl",              
    ) -> None:
        self.world_size = num_workers        
        self.backend = backend
        self.ds_config = ds_config if ds_config else json.loads(DEFUALT_CONFIG)
        self.train_args = train_args  
        self.get_model = get_model 
        self.data_refs = data_refs     
    
def _init_distributed_environment(
        parallel_config: ParallelConfig,
        rank: int,
        distributed_init_method: str        
    ) -> None:        
        if parallel_config.backend == "nccl":
            # Same as in Ray Train
            os.environ["NCCL_ASYNC_ERROR_HANDLING"] = "1"
            # All workers on a same node should share the same set of
            # visible GPUs. Otherwise they can't talk among themselves.
            # os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(gid) for gid in gpu_ids)
            if "NCCL_SOCKET_IFNAME" not in os.environ:
                os.environ["NCCL_SOCKET_IFNAME"] = DEFAULT_NCCL_SOCKET_IFNAME

        # os.environ["RANK"] = str(rank)
        # os.environ["LOCAL_RANK"] = str(rank)
        # os.environ["WORLD_SIZE"] = str(parallel_config.world_size)
        # os.environ["LOCAL_WORLD_SIZE"] = str(parallel_config.world_size)        
        print(f'''
deepspeed worker config:
              RANK:{rank} 
              WORLD_SIZE:{parallel_config.world_size}
              CUDA_VISIBLE_DEVICES:{os.environ["CUDA_VISIBLE_DEVICES"]} 
              LOCAL_RANK:{os.environ["LOCAL_RANK"]}
              LOCAL_WORLD_SIZE:{os.environ["LOCAL_WORLD_SIZE"]}
''',flush=True) 
        # torch.cuda.set_device(rank)
        """Initialize the distributed environment."""
        deepspeed.init_distributed(
            dist_backend="nccl",
            auto_mpi_discovery=False,
            verbose=True,
            init_method=distributed_init_method,
            rank=rank,
            world_size=parallel_config.world_size,
        )
        # torch.distributed.init_process_group(
        #     backend="nccl",
        #     world_size=parallel_config.world_size,
        #     rank=rank,
        #     init_method=distributed_init_method,            
        # )
        # # A small all_reduce for warmup.
        # torch.distributed.all_reduce(torch.zeros(1).cuda())

        

class ResourceWorker:
    def __init__(
        self,        
        parallel_config: ParallelConfig,        
        rank: int,
        distributed_init_method: str,
       
    ) -> None:
        self.parallel_config = parallel_config        
        self.rank = rank
        self.distributed_init_method = distributed_init_method        
        self.ds_config = self.parallel_config.ds_config

    def get_node_and_gpu_ids(self):
        """Returns the node and GPU ids of the current worker."""
        node_id, gpu_ids = _get_node_and_gpu_ids()
        return DeviceID(node_id, gpu_ids, self.rank)  

    def rank(self):
        return self.rank  
    
    def get_node_ip_address(self):
        return ray.util.get_node_ip_address()


class Worker:
    
    def __init__(
        self,        
        parallel_config: ParallelConfig,        
        rank: int,
        distributed_init_method: str,
       
    ) -> None:
        self.parallel_config = parallel_config        
        self.rank = rank
        self.distributed_init_method = distributed_init_method        
        self.ds_config = self.parallel_config.ds_config
        self.get_model = self.parallel_config.get_model
        self.model = None
        self.tokenizer = None
    
    def get_node_and_gpu_ids(self):
        """Returns the node and GPU ids of the current worker."""
        node_id, gpu_ids = _get_node_and_gpu_ids()
        return DeviceID(node_id, gpu_ids, self.rank)

    def _train(self,data_engine, model_engine):
        model_engine.train()
        step = 0
        while step < self.parallel_config.train_args.steps_per_epoch:
            data = data_engine.get_data()
            loss = model_engine(data, labels=data).loss
            model_engine.backward(loss)
            model_engine.step()
            step += 1
        return   
    
    def train(self):        
        model_engine = self.prepare_model()
        data_engine = self.prepare_data()
        epoch = 0
        while epoch < self.parallel_config.train_args.epoches:
            self._train(data_engine, model_engine)
            epoch += 1
            model_engine.save_checkpoint(f"{self.parallel_config.train_args.checkpoint_saving_path}",
                                        tag=f"Epoch-{epoch}")
    def prepare_data(self):
        data_dir = self.parallel_config.train_args.data_dir

        if self.parallel_config.data_refs: 
            train_file = os.path.join(data_dir,"train.txt")
            
            '''
            simplely write data to text file
            may need to be think how to handle for new line if the conversation contains new line. This is because
            the data_engine will get data line by line util touch the limit of max_length, then this seuqence will be
            used to train the model.
            But since this is for pretraining, it should be fine.
            '''
            with open(train_file,"w") as f: 
                count = 0
                for item in RayContext.collect_from(self.data_refs):                
                    if "conversation" in item:
                        item["conversation"] = item["conversation"].tolist()
                        s =  " ".join(conversation)
                        f.write(s+"\n")                    
                    elif "history" in item:
                        # support alpaca format data
                        conversation = [sub.tolist() for sub in item["history"].tolist()]
                        conversation = [{"human":x[0],"assistant":x[1]} for x in conversation]
                        latest_conversation = [{"human":item["instruction"],"assistant":item["output"]}] if "instruction" in item and item["instruction"] else []
                        s = " ".join(conversation) + " ".join(latest_conversation)
                        f.write(s+"\n")                     
                    count += 1  

        data_dir = self.parallel_config.train_args.data_dir
        tokenizer_path = self.parallel_config.train_args.tokenizer_path        
        micro_batch_size = self.ds_config["train_micro_batch_size_per_gpu"]
        max_length = self.parallel_config.train_args.max_length

        world_size = 1 if self.parallel_config.train_args.is_partition_data else self.parallel_config.world_size
        rank = 0 if self.parallel_config.train_args.is_partition_data else self.rank

        data_engine = DataEngine(data_dir, tokenizer_path, micro_batch_size, max_length,world_size,rank)
        data_engine.load_data()
        return data_engine     
    
    def prepare_model(self):
        # Initialize the distributed environment.
        _init_distributed_environment(self.parallel_config, self.rank,
                                      self.distributed_init_method)
        
        # check the enabled parameter here: https://github.com/microsoft/DeepSpeed/issues/3234
        
        with deepspeed.zero.Init(config_dict_or_path=self.ds_config,
                             enabled=self.ds_config["zero_optimization"]["stage"] == 3,
                             mem_efficient_linear=False,
                             mpu=None):
            model = self.get_model()            
            model_parameters = filter(lambda p: p.requires_grad, model.parameters())
            model_engine, _, _, _ = deepspeed.initialize(model=model,
                                                         config=self.ds_config,
                                                        optimizer=None,
                                                        model_parameters=model_parameters)
            return model_engine
            
           

class DeepSpeedTrain:
    def __init__(self,parallel_config: ParallelConfig):    

        master_addr, master_port = get_address_and_port()        
        distributed_init_method = f"tcp://{master_addr}:{master_port}"  
        print(f"deepspeed: master_addr:{master_addr},master_port:{master_port}",flush=True)

        # get resource manually. If use num_gpus, the ray will set cuda_visible_devices automatically, and this
        # will cause the deepspeed can't get the right gpu_ids enven if we set the cuda_visible_devices manually.                
        resource_workers = [] 
        workers = []       
        
        for rank in range(parallel_config.world_size):    
            worker_cls = ResourceWorker                        
            worker_cls = ray.remote(
                        num_cpus=0,
                        num_gpus=1,
                        # resources={f"node:{master_addr}": 1e-3},
                        # runtime_env=runtime_env,                        
                    )(worker_cls).remote
            worker = worker_cls(parallel_config,rank,distributed_init_method)
            resource_workers.append(worker)

        self.resource_workers  = resource_workers        
        self.node_id_to_workers = {}
        self.node_id_to_gpus = {}

        for resource_worker in self.resource_workers:
            device = ray.get(resource_worker.get_node_and_gpu_ids.remote())            
     
            if device.node_id not in self.node_id_to_workers:
                self.node_id_to_workers[device.node_id] = []
            
            if device.node_id not in self.node_id_to_gpus:
                self.node_id_to_gpus[device.node_id] = []    
            
            self.node_id_to_workers[device.node_id].append(resource_worker)    
            self.node_id_to_gpus[device.node_id].extend(device.gpu_ids)
            self.node_id_to_gpus[device.node_id].sort()

        for node_id, resource_workers in self.node_id_to_workers.items():
            for local_rank,resource_worker in enumerate(resource_workers):
                rank = ray.get(resource_worker.rank.remote()) 
                worker_cls = Worker  
                gpu_ids = self.node_id_to_gpus[node_id]
                env_vars = {
                        "RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES":"true",
                        "CUDA_VISIBLE_DEVICES": ",".join([str(gid) for gid in gpu_ids]),
                        "LOCAL_RANK": str(local_rank),
                        "RANK": str(rank),
                        "LOCAL_WORLD_SIZE": str(len(gpu_ids)),
                        "WORLD_SIZE": str(parallel_config.world_size)
                        }                                        
                runtime_env = {"env_vars": env_vars}  
                node_ip = ray.get(resource_worker.get_node_ip_address.remote())  
                worker_cls = ray.remote(
                            num_cpus=0,
                            num_gpus=0,
                            resources={f"node:{node_ip}": 1e-3},
                            runtime_env=runtime_env,                        
                        )(worker_cls).remote
                worker = worker_cls(parallel_config,rank,distributed_init_method)
                workers.append(worker)  
        self.workers = workers                              
    
              
    def _run_workers(
        self,
        method: str,
        *args,
        get_all_outputs: bool = False,
        **kwargs,
    ) -> Any:
        """Runs the given method on all workers."""
        all_outputs = []
        for worker in self.workers:
            executor = getattr(worker, method)
            executor = executor.remote
            output = executor(*args, **kwargs)
            all_outputs.append(output)

        all_outputs = ray.get(all_outputs)            
        
        if get_all_outputs:
            return all_outputs

        # Make sure all workers have the same results.
        output = all_outputs[0]
        for other_output in all_outputs[1:]:
            assert output == other_output
        return output 


def sfft_train(data_refs:List[DataServer],train_params:Dict[str,str],sys_conf: Dict[str, str])->Generator[BlockRow,Any,Any]:
    import datetime
    import uuid
        
    localPathPrefix = train_params.get("localPathPrefix","/tmp/byzerllm")
    
    current_time = datetime.datetime.now()
    formatted_time = current_time.strftime("%Y%m%d-%H%-M-%S")
    rd = f"sft-{formatted_time}-{str(uuid.uuid4())}"

    sft_name = train_params["name"] if "name" in train_params else f"sft-{sys_conf['OWNER']}"

    num_gpus = int(sys_conf.get("num_gpus",0))
    
    assert num_gpus > 0, 'num_gpus must be greater than 0. Try to fix it with `!byzerllm setup "num_gpus=4"`'
    
    data_dir = train_params["data_dir"] if "data_dir" in train_params else os.path.join(localPathPrefix,rd,"finetune_data")
    output_dir = os.path.join(localPathPrefix,rd,"finetune_model")
    
    model_dir = os.path.join(localPathPrefix,rd,"pretrained_model")
    
    if "localModelDir" in train_params:
        model_dir = train_params["localModelDir"]


    def get_model():
        return AutoModelForCausalLM.from_pretrained(model_dir,trust_remote_code=True)
    

    dst = DeepSpeedTrain(ParallelConfig(
    num_workers=num_gpus,
    get_model = get_model,
    ds_config=  json.loads(train_params.get("ds_config","{}")), 
    train_args=TrainArgs(
        model_path=model_dir,
        tokenizer_path = f"{model_dir}/tokenizer.model",
        data_dir = data_dir,  
        checkpoint_saving_path = output_dir,   
        steps_per_epoch = int(train_params.get("steps_per_epoch",10)),
        max_length = int(train_params.get("max_length",4096)),
        epoches=int(train_params.get("epoches",1)),
        is_partition_data = len(data_refs) != 0
        )
    ))

    ray.get([worker.train.remote() for worker in dst.workers])



