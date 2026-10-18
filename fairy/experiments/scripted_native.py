"""Offline-only function-calling fixture, never selected by live configuration."""
from __future__ import annotations

import json

from openai.types.chat import ChatCompletion

from fairy.agents.Jev.candidates import extract_task_parameters


class NativeFixtureLLM:
    provider="offline_fixture"
    model="scripted-native-fixture"
    parallel_tool_calls=True
    max_output_tokens=2048
    temperature=0.1

    def __init__(self,runtime):
        self.runtime=runtime
        self.index=0
        self.last_request_id=None

    def chat_completion(self,messages,tools=None):
        self.index+=1
        self.last_request_id=f"scripted_native_{self.index:04d}"
        task=next(m['content'] for m in messages if m.get('role')=='user')
        params=extract_task_parameters(task)
        start,end=params['target_range']
        def call(name,args={}):return (name,args)
        if self.index==1:
            calls=[call('WeatherApp__get_current_weather'),call('SensorApp__read_soil_sensors'),call('FarmWorldApp__get_ridge_range_state',{'start':start,'end':end}),call('FarmWorldApp__get_inventory')]
        elif self.index==2:
            calls=[call('FieldOpsApp__irrigate',{'start':start,'end':end,'hours':params['irrigation_hours']})]
        elif self.index==3:
            rows=[e for e in self.runtime.trace.events if e['event']=='tool_end' and isinstance(e.get('result'),dict) and e['result'].get('effect_ready_at')]
            last=rows[-1]
            import math
            wait=max(1,math.ceil(last['result']['effect_ready_at']-last['sim_time_after']))
            calls=[call('SystemApp__advance_time',{'seconds':wait,'minutes':0,'hours':0,'days':0})]
        elif self.index==4:
            calls=[call('FarmWorldApp__get_ridge_range_state',{'start':start,'end':end}),call('SensorApp__read_soil_sensors'),call('FarmWorldApp__get_inventory')]
        elif self.index==5:
            calls=[call('AgentUserInterface__send_message_to_user',{'content':f'离线控制实验：{start}–{end} 垄已按请求灌溉，等待响应后已复查目标区与土壤水分，配额结果已记录。'})]
        else:calls=[]
        message={'role':'assistant','content':None if calls else '离线控制实验已完成灌溉和等待后复查。'}
        if calls:message['tool_calls']=[{'id':f'fixture_{self.index}_{i}','type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}} for i,(name,args) in enumerate(calls)]
        self.runtime.trace.emit('native_scripted_response',fixture=True,request_id=self.last_request_id,messages=messages,response=message,model_http_attempts=0)
        return ChatCompletion.model_validate({'id':self.last_request_id,'created':0,'object':'chat.completion','model':self.model,'choices':[{'index':0,'finish_reason':'tool_calls' if calls else 'stop','message':message}]})
