from transformers import AutoTokenizer, AutoModelForCausalLM
import transformers
import torch
from typing import Dict,List,Tuple



def stream_chat(self,tokenizer,ins:str, his:List[Tuple[str,str]]=[],  
        max_length:int=1024, 
        top_p:float=0.95,
        temperature:float=0.1,**kwargs):
        
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    user_role = kwargs.get("userRole","User")
    assistant_role = kwargs.get("assistantRole","Assistant")

    his_str = []
    for item in his:
        his_str.append(f"{user_role}:{item[0]}")
        his_str.append(f"{assistant_role}:{item[1]}")
    user_ins = ins if len(his_str)==0 or ins.startWith(f"{user_role}:") else f"{user_role}:{ins}\n{assistant_role}:"
    fin_ins = his_str.join("\n") + user_ins

    tokens = tokenizer(fin_ins, return_token_type_ids=False,return_tensors="pt").to(device)
    response = self.generate(
        input_ids=tokens["input_ids"],
        max_new_tokens=max_length,
        repetition_penalty=1.05,
        temperature=temperature,
        eos_token_id=tokenizer.eos_token_id
    )
    answer = tokenizer.decode(response[0][tokens["input_ids"].shape[1]:], skip_special_tokens=True)
    return [(answer,"")]


def init_model(model_dir,infer_params:Dict[str,str]={},sys_conf:Dict[str,str]={}):        
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    tokenizer.padding_side="right"
    tokenizer.pad_token_id=0
    model = AutoModelForCausalLM.from_pretrained(model_dir,trust_remote_code=True,
                                                device_map='auto',                                                
                                                torch_dtype=torch.bfloat16                                                
                                                )
    model.eval()       
    import types
    model.stream_chat = types.MethodType(stream_chat, model)     
    return (model,tokenizer)


