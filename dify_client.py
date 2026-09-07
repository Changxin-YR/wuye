"""Server-side Dify /chat-messages adapter, including Agent SSE replies.
Tool thought/reasoning events are ignored; no raw upstream errors are exposed.
"""
import json
import os
import time
import uuid
from urllib.parse import urlsplit
import requests

class DifyUnavailable(RuntimeError):
    def __init__(self,message,code='unavailable'):
        super().__init__(message);self.code=code


class BailianClient:
    """Alibaba Bailian/Qwen OpenAI-compatible chat client."""
    def __init__(self,base_url,api_key,model='qwen-plus',timeout=60):
        self.base_url=(base_url or '').rstrip('/')
        self.api_key=(api_key or '').strip()
        self.model=(model or 'qwen-plus').strip()
        self.timeout=max(1,min(int(timeout),120))

    @property
    def configured(self):
        return bool(self.api_key and not self.api_key.startswith(('sk-your','your-','replace-')) and self.base_url and self.model)

    def _validate_url(self):
        parsed=urlsplit(self.base_url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
            raise DifyUnavailable('百炼地址配置不正确，请管理员检查。','configuration')

    def _request(self,method,path,payload=None):
        if not self.configured:raise DifyUnavailable('AI尚未配置百炼 API Key，请联系管理员。','not_configured')
        self._validate_url()
        try:
            with requests.request(method,self.base_url+path,headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json'},json=payload,timeout=(5,self.timeout),allow_redirects=False) as r:
                if r.status_code in {401,403}:raise DifyUnavailable('百炼 API Key 无效或权限不足，请联系管理员。','authentication')
                if r.status_code==429:raise DifyUnavailable('百炼服务繁忙，请稍后重试。','rate_limit')
                if not 200<=r.status_code<300:raise DifyUnavailable('百炼服务返回异常，请管理员检查模型配置。','upstream')
                obj=r.json()
                if not isinstance(obj,dict):raise DifyUnavailable('百炼返回格式异常。','bad_response')
                return obj
        except requests.Timeout as exc:raise DifyUnavailable('百炼响应超时，报修功能仍可正常使用。','timeout') from exc
        except requests.RequestException as exc:raise DifyUnavailable('无法连接百炼服务，请管理员检查网络和 API 地址。','connection') from exc
        except (ValueError,UnicodeError) as exc:raise DifyUnavailable('百炼返回了无法解析的内容。','bad_response') from exc

    def _stream_completion(self,payload):
        if not self.configured:raise DifyUnavailable('AI尚未配置百炼 API Key，请联系管理员。','not_configured')
        self._validate_url()
        try:
            with requests.request('POST',self.base_url+'/chat/completions',headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json'},json=payload,timeout=(5,self.timeout),allow_redirects=False,stream=True) as r:
                if r.status_code in {401,403}:raise DifyUnavailable('百炼 API Key 无效或权限不足，请联系管理员。','authentication')
                if r.status_code==429:raise DifyUnavailable('百炼服务繁忙，请稍后重试。','rate_limit')
                if not 200<=r.status_code<300:raise DifyUnavailable('百炼服务返回异常，请管理员检查模型配置。','upstream')
                content_type=r.headers.get('Content-Type','')
                if 'text/event-stream' not in content_type:raise DifyUnavailable('百炼未返回SSE流，请检查模型配置。','bad_response')
                r.encoding='utf-8'
                started=time.monotonic();size=0;parts=[];calls={};response_id='';done=False
                for line in r.iter_lines(chunk_size=512,decode_unicode=True):
                    size+=len(line or '')
                    if size>2_000_000:raise DifyUnavailable('AI响应过长，请缩小问题范围。','bad_response')
                    if time.monotonic()-started>self.timeout:raise DifyUnavailable('百炼响应超时，请稍后重试。','timeout')
                    if not line or not line.startswith('data:'):continue
                    raw=line[5:].lstrip()
                    if raw=='[DONE]':done=True;break
                    obj=json.loads(raw)
                    if not isinstance(obj,dict):raise ValueError()
                    response_id=response_id or obj.get('id','')
                    choices=obj.get('choices')
                    if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):raise ValueError()
                    delta=choices[0].get('delta') or {}
                    if not isinstance(delta,dict):raise ValueError()
                    content=delta.get('content')
                    if isinstance(content,str) and content:
                        parts.append(content);yield {'type':'delta','content':content}
                    fragments=delta.get('tool_calls') or []
                    if not isinstance(fragments,list):raise ValueError()
                    for fragment in fragments:
                        if not isinstance(fragment,dict):raise ValueError()
                        index=fragment.get('index',0)
                        if not isinstance(index,int) or index<0 or index>4:raise ValueError()
                        call=calls.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                        if isinstance(fragment.get('id'),str) and not call['id']:call['id']=fragment['id']
                        fn=fragment.get('function') or {}
                        if not isinstance(fn,dict):raise ValueError()
                        if isinstance(fn.get('name'),str):call['function']['name']+=fn['name']
                        if isinstance(fn.get('arguments'),str):call['function']['arguments']+=fn['arguments']
                if not done:raise DifyUnavailable('百炼流式回答不完整，请重试。','bad_response')
                message={'role':'assistant','content':''.join(parts) or None}
                if calls:message['tool_calls']=[calls[i] for i in sorted(calls)]
                return {'id':response_id,'message':message}
        except requests.Timeout as exc:raise DifyUnavailable('百炼响应超时，报修功能仍可正常使用。','timeout') from exc
        except requests.RequestException as exc:raise DifyUnavailable('无法连接百炼服务，请管理员检查网络和 API 地址。','connection') from exc
        except (ValueError,UnicodeError) as exc:raise DifyUnavailable('百炼返回了无法解析的内容。','bad_response') from exc

    def chat_stream(self,query,user,conversation_id='',tool_callback=None,system_prompt=''):
        messages=([{'role':'system','content':system_prompt}] if isinstance(system_prompt,str) and system_prompt.strip() else [])+[{'role':'user','content':query}]
        seen_tool_calls=set()
        tools=[{'type':'function','function':{'name':'property_agent_tool','description':'查询授权物业数据或办理业务。command 必须使用 context 返回的 queries 或 commands，禁止猜测隐藏命令；高风险操作只生成待确认卡片。','parameters':{'type':'object','properties':{'request_token':{'type':'string'},'operation':{'type':'string','enum':['context','lookup','execute','propose']},'command':{'type':'string','description':'仅使用 context.queries 或 context.commands 中的命令'},'arguments_json':{'type':'string','description':'JSON 对象；先 lookup 获取真实 id/version，再执行或 propose'}},'required':['operation']}}}]
        for _ in range(6):
            payload={'model':self.model,'messages':messages,'stream':True}
            if tool_callback:payload['tools']=tools;payload['tool_choice']='auto'
            completion=yield from self._stream_completion(payload)
            message=completion.get('message') or {}
            if not isinstance(message,dict):raise DifyUnavailable('百炼返回了无法识别的回答。','bad_response')
            calls=message.get('tool_calls') or []
            if calls and tool_callback:
                if not isinstance(calls,list) or len(calls)>4:raise DifyUnavailable('百炼返回的工具调用过多。','bad_response')
                messages.append(message)
                for call in calls:
                    try:
                        fn=call['function'];args=json.loads(fn['arguments'])
                        if fn.get('name')!='property_agent_tool' or not isinstance(args,dict):raise ValueError()
                        signature=json.dumps([fn.get('name'),fn.get('arguments')],ensure_ascii=False,sort_keys=True)
                        if signature in seen_tool_calls:raise DifyUnavailable('百炼重复调用相同工具，请重试。','tool_loop')
                        seen_tool_calls.add(signature)
                        result=tool_callback(args)
                    except (KeyError,TypeError,ValueError,UnicodeError):result={'error':'工具调用参数无效'}
                    messages.append({'role':'tool','tool_call_id':str(call.get('id','')),'content':json.dumps(result,ensure_ascii=False,default=str)})
                continue
            answer=message.get('content')
            if not isinstance(answer,str) or not answer.strip():raise DifyUnavailable('百炼未返回有效文本。','bad_response')
            yield {'type':'done','answer':answer,'conversation_id':conversation_id or completion.get('id') or str(uuid.uuid4())}
            return
        raise DifyUnavailable('百炼工具调用次数超出限制，请重试。','bad_response')

    def chat(self,query,user,conversation_id='',tool_callback=None,system_prompt=''):
        messages=([{'role':'system','content':system_prompt}] if isinstance(system_prompt,str) and system_prompt.strip() else [])+[{'role':'user','content':query}]
        seen_tool_calls=set()
        tools=[{'type':'function','function':{'name':'property_agent_tool','description':'查询授权物业数据或办理业务。command 必须使用 context 返回的 queries 或 commands，禁止猜测隐藏命令；高风险操作只生成待确认卡片。','parameters':{'type':'object','properties':{'request_token':{'type':'string'},'operation':{'type':'string','enum':['context','lookup','execute','propose']},'command':{'type':'string','description':'仅使用 context.queries 或 context.commands 中的命令'},'arguments_json':{'type':'string','description':'JSON 对象；先 lookup 获取真实 id/version，再执行或 propose'}},'required':['operation']}}}]
        for _ in range(6):
            payload={'model':self.model,'messages':messages,'stream':False}
            if tool_callback:payload['tools']=tools;payload['tool_choice']='auto'
            obj=self._request('POST','/chat/completions',payload)
            choices=obj.get('choices')
            if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):raise DifyUnavailable('百炼返回了无法识别的回答。','bad_response')
            message=choices[0].get('message') or {}
            if not isinstance(message,dict):raise DifyUnavailable('百炼返回了无法识别的回答。','bad_response')
            calls=message.get('tool_calls') or []
            if calls and tool_callback:
                if not isinstance(calls,list) or len(calls)>4:raise DifyUnavailable('百炼返回的工具调用过多。','bad_response')
                messages.append(message)
                for call in calls:
                    try:
                        fn=call['function'];args=json.loads(fn['arguments'])
                        if fn.get('name')!='property_agent_tool' or not isinstance(args,dict):raise ValueError()
                        signature=json.dumps([fn.get('name'),fn.get('arguments')],ensure_ascii=False,sort_keys=True)
                        if signature in seen_tool_calls:raise DifyUnavailable('百炼重复调用相同工具，请重试。','tool_loop')
                        seen_tool_calls.add(signature)
                        result=tool_callback(args)
                    except (KeyError,TypeError,ValueError,UnicodeError):result={'error':'工具调用参数无效'}
                    messages.append({'role':'tool','tool_call_id':str(call.get('id','')),'content':json.dumps(result,ensure_ascii=False,default=str)})
                continue
            answer=message.get('content')
            if not isinstance(answer,str) or not answer.strip():raise DifyUnavailable('百炼未返回有效文本。','bad_response')
            return {'answer':answer,'conversation_id':conversation_id or obj.get('id') or str(uuid.uuid4())}
        raise DifyUnavailable('百炼工具调用次数超出限制，请重试。','bad_response')

    def check(self,infer=False):
        obj=self._request('GET','/models')
        if not isinstance(obj.get('data'),list):raise DifyUnavailable('百炼模型列表返回格式异常。','bad_response')
        if infer:self.chat('这是连接测试，请只回答：连接成功。','property-healthcheck')
        return {'status':'ok','app_mode':'chat','model':self.model,'inference_checked':infer,'agent_tools_verified':False,'message':'百炼 API 与模型推理正常；Agent 工具需另做联调' if infer else '百炼 API Key 和模型配置有效'}

class DifyClient:
    def __init__(self,base_url,api_key,timeout=60):
        self.base_url=(base_url or '').rstrip('/');self.api_key=(api_key or '').strip()
        self.timeout=max(1,min(int(timeout),120))
    @property
    def configured(self):return bool(self.api_key and not self.api_key.startswith('app-your') and self.base_url)
    def _request(self,method,path,payload=None,stream=False):
        if not self.configured:raise DifyUnavailable('AI尚未配置，请联系管理员。','not_configured')
        parsed=urlsplit(self.base_url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
            raise DifyUnavailable('AI地址配置不正确，请管理员检查。','configuration')
        try:
            with requests.request(method,self.base_url+path,headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json'},json=payload,timeout=(5,self.timeout),allow_redirects=False,stream=stream) as r:
                if r.status_code in {401,403}:raise DifyUnavailable('AI应用密钥无效或权限不足，请联系管理员。','authentication')
                if r.status_code==429:raise DifyUnavailable('AI服务繁忙，请稍后重试。','rate_limit')
                if not 200<=r.status_code<300:raise DifyUnavailable('AI服务返回异常，请管理员检查已发布的应用。','upstream')
                if stream:
                    if 'text/event-stream' not in r.headers.get('Content-Type',''):raise DifyUnavailable('Agent未返回SSE流，请检查应用接口。','bad_response')
                    return self._stream(r)
                obj=r.json()
                if not isinstance(obj,dict):raise DifyUnavailable('AI返回格式异常。','bad_response')
                return obj
        except requests.Timeout as exc:raise DifyUnavailable('AI响应超时，报修功能仍可正常使用。','timeout') from exc
        except requests.RequestException as exc:raise DifyUnavailable('无法连接AI服务，请管理员检查Dify及模型服务。','connection') from exc
        except (ValueError,UnicodeError) as exc:raise DifyUnavailable('AI返回了无法解析的内容。','bad_response') from exc
    def _stream(self,response):
        started=time.monotonic();chunks=[];cid='';ended=False;size=0;event_lines=[]
        response.encoding='utf-8'
        for line in response.iter_lines(chunk_size=512,decode_unicode=True):
            size+=len(line)
            if size>2_000_000:raise DifyUnavailable('AI响应过长，请缩小问题范围。','bad_response')
            if time.monotonic()-started>self.timeout:raise DifyUnavailable('AI响应超时，请稍后重试。','timeout')
            if line.startswith('data:'):event_lines.append(line[5:].lstrip())
            elif not line and event_lines:
                raw='\n'.join(event_lines);event_lines=[]
                if raw=='[DONE]':break
                obj=json.loads(raw)
                if not isinstance(obj,dict):raise ValueError()
                event=obj.get('event')
                if event=='error':raise DifyUnavailable('Agent执行失败，请管理员查看Dify运行记录。','upstream')
                if event in {'message','agent_message','message_replace','message_end'}:
                    newcid=obj.get('conversation_id')
                    if newcid:
                        if not isinstance(newcid,str) or len(newcid)>128 or (cid and cid!=newcid):raise ValueError()
                        cid=newcid
                    if event in {'message','agent_message','message_replace'}:
                        answer=obj.get('answer')
                        if not isinstance(answer,str):raise ValueError()
                        if event=='message_replace':chunks=[answer]
                        else:chunks.append(answer)
                    if event=='message_end':ended=True;break
        answer=''.join(chunks)
        if not ended or not answer.strip() or not cid:raise DifyUnavailable('AI流式回答不完整，请重试。','bad_response')
        return {'answer':answer,'conversation_id':cid}
    def check(self,infer=False):
        obj=self._request('GET','/info');mode=obj.get('mode')
        if mode not in {'chat','agent-chat','advanced-chat'}:raise DifyUnavailable('该应用不使用受支持的chat-messages接口，请核对Dify API访问页。','app_mode')
        if infer:self.chat('这是连接测试，请不要使用任何工具，只回答：连接成功。','property-healthcheck')
        return {'status':'ok','app_mode':mode,'inference_checked':infer,'agent_tools_verified':False,
                'message':'模型已返回有效文本；业务工具须另做联调' if infer else 'Dify接口与密钥有效，尚未验证模型推理'}
    def chat(self,query,user,conversation_id=''):
        payload={'inputs':{},'query':query,'response_mode':'streaming','user':user}
        if conversation_id:payload['conversation_id']=conversation_id
        return self._request('POST','/chat-messages',payload,stream=True)

def chat(message,user_id,role):
    return DifyClient(os.getenv('DIFY_BASE_URL','http://127.0.0.1/v1'),os.getenv('DIFY_API_KEY','')).chat(message,f'property:{user_id}:{role}')['answer']


def client_from_env():
    if os.getenv('AI_PROVIDER','bailian').strip().lower()=='dify':
        return DifyClient(os.getenv('DIFY_BASE_URL','http://127.0.0.1/v1'),os.getenv('DIFY_API_KEY',''),os.getenv('DIFY_TIMEOUT','60'))
    return BailianClient(os.getenv('BAILIAN_BASE_URL','https://dashscope.aliyuncs.com/compatible-mode/v1'),os.getenv('BAILIAN_API_KEY') or os.getenv('DASHSCOPE_API_KEY',''),os.getenv('BAILIAN_MODEL','qwen-plus'),os.getenv('DIFY_TIMEOUT','60'))
