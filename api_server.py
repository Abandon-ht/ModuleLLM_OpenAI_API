import os
import uuid
import yaml
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import logging
from slowapi import Limiter
from slowapi.util import get_remote_address
import time
import json
import asyncio
from aiostream import stream
from llm_client import LLMClient

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("api")

app = FastAPI(title="OpenAI Compatible API Server")
limiter = Limiter(key_func=get_remote_address)

class Config:
    def __init__(self):
        with open("config.yaml") as f:
            self.data = yaml.safe_load(f)

config = Config()

class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1000
    stream: Optional[bool] = False

class CompletionRequest(BaseModel):
    model: str
    prompt: str
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1000
    stream: Optional[bool] = False

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/v1"):
        api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
        if api_key != os.getenv("API_KEY"):
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid authentication credentials"}
            )
    return await call_next(request)

class BaseModelBackend:
    def __init__(self, model_config):
        self.config = model_config

    async def generate(self, request: ChatCompletionRequest):
        raise NotImplementedError

class TestBackend(BaseModelBackend):
    async def generate(self, request: ChatCompletionRequest):
        if request.stream:
            async def chunk_generator():
                content_parts = ["🤣", "👉🏻", "🤡"]
                messages=[m.model_dump() for m in request.messages]
                print(f"messages:_____________{messages}______________")
                for i, part in enumerate(content_parts):
                    yield {
                        "id": f"chatcmpl-{uuid.uuid4()}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "content": part,
                                "role": "assistant" if i == 0 else None,
                                "function_call": None,
                                "tool_calls": None
                            },
                            "logprobs": None,
                            "finish_reason": "stop" if i == len(content_parts)-1 else None
                        }],
                        "service_tier": None,
                        "system_fingerprint": None,
                        "usage": None
                    }
            return chunk_generator()
        else:
            return {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model,
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "🤣👉🏻🤡",
                        "function_call": None,
                        "tool_calls": None
                    },
                    "finish_reason": "stop",
                    "index": 0
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30
                }
            }

class OpenAIProxyBackend(BaseModelBackend):
    async def generate(self, request: ChatCompletionRequest):
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["base_url"]
        )
        
        response = await client.chat.completions.create(
            model=self.config["model"],
            messages=[m.model_dump() for m in request.messages],
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=request.stream
        )
        
        if request.stream:
            async def async_wrapper():
                async for chunk in response:
                    yield chunk
            return async_wrapper()
        return response

class LlmClientBackend(BaseModelBackend):
    MAX_CONTEXT_LENGTH = 500
    POOL_SIZE = 2  # 新增连接池大小限制

    def __init__(self, model_config):
        super().__init__(model_config)
        self._client_pool = []       # 可用连接池
        self._active_clients = {}    # 使用中的连接
        self._pool_lock = asyncio.Lock()
        self.logger = logging.getLogger("api.client")

    async def _get_client(self, request):
        async with self._pool_lock:
            # 尝试从池中获取可用连接
            if self._client_pool:
                client = self._client_pool.pop()
                self.logger.debug(f"♻️ Reusing client from pool | ID:{id(client)}")
                return client

            # 检查是否达到最大连接数
            if len(self._active_clients) >= self.POOL_SIZE:
                raise RuntimeError("Connection pool exhausted")

            # 创建新连接
            self.logger.debug("🆕 Creating new LLM client")
            client = LLMClient(
                host=self.config["host"],
                port=self.config["port"]
            )
            self._active_clients[id(client)] = client
            
            # 初始化连接
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, 
                client.setup, 
                {
                    "model": self.config["model_name"],
                    "response_format": "llm.utf-8.stream",
                    "input": "llm.utf-8",
                    "enoutput": True,
                    "max_token_len": request.max_tokens,
                    "temperature": request.temperature,
                    "prompt": next(
                        (m.content for m in request.messages if m.role == "system"),
                        self.config.get("system_prompt", "You are a helpful assistant")
                    )
                }
            )
            return client

    async def _release_client(self, client):
        async with self._pool_lock:
            # 将连接放回池中供后续使用
            self._client_pool.append(client)
            self.logger.debug(f"🔙 Returned client to pool | ID:{id(client)}")

    async def inference_stream(self, query: str, request: ChatCompletionRequest):
        client = await self._get_client(request)
        try:
            self.logger.debug(f"📡 Starting inference | ClientID:{id(client)} Query length:{len(query)}")
            
            loop = asyncio.get_event_loop()
            sync_gen = client.inference_stream(query)
            
            while True:
                try:
                    # 使用闭包捕获生成器状态
                    def get_next():
                        try:
                            return next(sync_gen)
                        except StopIteration:
                            return None  # 返回哨兵值代替抛出异常
                            
                    chunk = await loop.run_in_executor(None, get_next)
                    if chunk is None:  # 检测到生成器结束
                        break
                    yield chunk
                except Exception as e:
                    self.logger.error(f"Inference error: {str(e)}")
                    yield f"[ERROR: {str(e)}]"
                    break
        finally:
            await self._release_client(client)

    def _truncate_history(self, messages: List[Message]) -> List[Message]:
        """Truncate history to fit model context window"""
        total_length = 0
        keep_messages = []
        
        # Process in reverse to keep latest messages
        for msg in reversed(messages):
            if msg.role == "system":  # Always keep system messages
                keep_messages.insert(0, msg)
                continue
                
            msg_length = len(msg.content)
            if total_length + msg_length > self.MAX_CONTEXT_LENGTH:
                break
            total_length += msg_length
            keep_messages.insert(0, msg)  # Maintain original order
            
        return keep_messages

    async def generate(self, request: ChatCompletionRequest):
        try:
            truncated_messages = self._truncate_history(request.messages)
            
            query = "\n".join([
                f"{m.role}: {m.content}" 
                for m in truncated_messages 
                if m.role != "system"
            ])
            
            self.logger.debug(
                f"Context truncated: Original {len(request.messages)} → Kept {len(truncated_messages)} "
                f"Total length:{len(query)} chars"
            )
            
            if request.stream:
                async def chunk_generator():
                    try:
                        async for chunk in self.inference_stream(query, request):
                            yield {
                                "id": f"chatcmpl-{uuid.uuid4()}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": request.model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": chunk},
                                    "finish_reason": None
                                }]
                            }
                        # Add normal completion marker
                        yield {
                            "choices": [{
                                "delta": {},
                                "finish_reason": "stop"
                            }]
                        }
                    except Exception as e:
                        self.logger.error(f"Stream generation error: {str(e)}")
                        yield {
                            "error": {
                                "message": f"Stream generation failed: {str(e)}",
                                "type": "api_error"
                            }
                        }
                        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                        raise
                return chunk_generator()
            else:
                full_response = ""
                async for chunk in self.inference_stream(query, request):
                    full_response += chunk
                return {
                    "id": f"chatcmpl-{uuid.uuid4()}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": request.model,
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": full_response
                        }
                    }]
                }
        except RuntimeError as e:
            self.logger.error(f"Connection error: {str(e)}")
            raise HTTPException(
                status_code=400,
                detail=f"Model service connection failed: {str(e)}"
            )

class ModelDispatcher:
    def __init__(self):
        self.backends = {}
        self.load_models()

    def load_models(self):
        for model_name, model_config in config.data["models"].items():
            if model_config["type"] == "openai_proxy":
                self.backends[model_name] = OpenAIProxyBackend(model_config)
            elif model_config["type"] == "tcp_client":
                self.backends[model_name] = LlmClientBackend(model_config)
            elif model_config["type"] == "llama.cpp":
                self.backends[model_name] = TestBackend(model_config)

    def get_backend(self, model_name):
        return self.backends.get(model_name)

_dispatcher = ModelDispatcher()

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    backend = _dispatcher.get_backend(body.model)
    if not backend:
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported model: {body.model}"
        )
    
    try:
        print(f"Received request: {body.model_dump()}")
        
        if body.stream:
            chunk_generator = await backend.generate(body)
            if not chunk_generator:
                raise HTTPException(
                    status_code=500,
                    detail="Failed to generate stream response"
                )
            
            async def format_stream():
                try:
                    async for chunk in chunk_generator:
                        if isinstance(chunk, dict):
                            chunk_dict = chunk
                        else:
                            chunk_dict = chunk.model_dump()
                            
                        json_chunk = json.dumps(chunk_dict, ensure_ascii=False)
                        print(f"Sending chunk: {json_chunk}")
                        yield f"data: {json_chunk}\n\n"
                except Exception as e:
                    logger.error(f"Stream interrupted: {str(e)}")
                    yield f"data: {{'error': 'Stream interrupted'}}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                format_stream(),
                media_type="text/event-stream"
            )
        else:
            response = await backend.generate(body)
            print(f"Sending response: {response}")
            return JSONResponse(content=response)
        
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Processing error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/completions")
async def create_completion(request: Request, body: CompletionRequest):
    chat_request = ChatCompletionRequest(
        model=body.model,
        messages=[Message(role="user", content=body.prompt)],
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        stream=body.stream
    )
    
    backend = _dispatcher.get_backend(chat_request.model)
    if not backend:
        raise HTTPException(status_code=400, detail=f"Unsupported model: {chat_request.model}")

    try:
        if body.stream:
            chunk_generator = await backend.generate(chat_request)
            
            async def convert_stream():
                async for chunk in chunk_generator:
                    # 转换格式后需要序列化为JSON字符串
                    completion_chunk = {
                        "id": chunk.get("id", f"cmpl-{uuid.uuid4()}"),
                        "object": "text_completion.chunk",
                        "created": chunk.get("created", int(time.time())),
                        "model": chat_request.model,
                        "choices": [{
                            "text": chunk["choices"][0]["delta"].get("content", ""),
                            "index": 0,
                            "logprobs": None,
                            "finish_reason": chunk["choices"][0].get("finish_reason")
                        }]
                    }
                    # 添加SSE格式包装
                    yield f"data: {json.dumps(completion_chunk)}\n\n"
                
                # 添加流结束标记
                yield "data: [DONE]\n\n"
            
            return StreamingResponse(
                convert_stream(),
                media_type="text/event-stream"
            )
        else:
            chat_response = await backend.generate(chat_request)
            return JSONResponse({
                "id": f"cmpl-{uuid.uuid4()}",
                "object": "text_completion",
                "created": int(time.time()),
                "model": chat_request.model,
                "choices": [{
                    "text": chat_response["choices"][0]["message"]["content"],
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop"
                }],
                "usage": chat_response.get("usage", {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                })
            })
            
    except Exception as e:
        logger.error(f"Completion error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

logging.getLogger().handlers[0].flush()