import json
import logging
import re
import traceback
from datetime import datetime
from typing import Union
from fastapi import Request
from fastapi import Response
from fastapi.responses import StreamingResponse

EXCLUDE_PATHS = {
    r".+\/live",
    r".+\/ready",
    r".+\/healthcheck",
    r".+\/docs",
    r".+\/openapi.json",
    r".+\/favicon.ico",
}


class HTTPMiddleware:

    @classmethod
    async def log(cls, event_method: str, event: str, context: dict, logger=None) -> None:
        if logger:
            await getattr(logger, event_method.lower())(event, **context)

    @classmethod
    def init_context(cls, request: Request) -> dict:
        context = {
            "method": request.method,
            "query_params": {k: v for k, v in request.query_params.items()},
            "timestamp": datetime.utcnow(),
            "level_name": "INFO",
            "level": logging.INFO,
            "url": request.url,
            "url_mask": request.url,
            "request_path": request.url.path,
            "headers": {k: v for k, v in request.headers.items()},
        }

        if request.scope.get("app", None):
            if request.scope["app"].title:
                context["facility"] = request.scope["app"].title

        if context.get("facility", None):
            context["action_name"] = f"{context['facility']}.{HTTPMiddleware.__name__}"
        else:
            context["action_name"] = HTTPMiddleware.__name__

        return context

    @classmethod
    async def parse_body(cls, request: Request) -> Union[str, bytes]:
        async def __receive() -> dict:
            """
            Статический результат работы request._receive который запускается в await request.body().

            Необходимо для возможности просмотра body из входящего запроса иначе приложение блокируется.
            """
            return {"type": "http.request", "body": body}

        body: bytes = await request.body()

        request._receive = __receive

        payload = body
        if not payload:
            return payload

        if request.headers.get("Content-Type") == "application/json":
            try:
                payload = json.loads(body.decode("utf-8").replace("\n", ""))
            except json.JSONDecodeError:
                # FastAPI 422 Error: Unprocessable Entity
                ...

        return payload

    @classmethod
    async def parse_response(cls, response: StreamingResponse) -> str:
        content = b""
        async for chunk in response.body_iterator:
            content += chunk
        return content.decode("utf-8")

    @classmethod
    async def _proxy(cls, request: Request, call_next, logger=None) -> Response:
        context = cls.init_context(request)

        if request.method in {"PATCH", "POST", "PUT"}:
            context["request_data"] = await cls.parse_body(request)

        await cls.log(
            event_method=context["level_name"],
            event=f"HTTP Request {context['url']}",
            context=context,
            logger=logger,
        )

        try:
            response = await call_next(request)
        except Exception as e:
            context["level_name"] = "ERROR"
            context["level"] = logging.ERROR
            context["exception"] = str(e)
            context["stack_trace"] = "".join(line for line in (traceback.format_tb(e.__traceback__)))
            context["status_code"] = 500

            await cls.log(
                event_method=context["level_name"],
                event=f"HTTP Error {context['url']}",
                context=context,
                logger=logger,
            )
            raise e

        finally:
            route = request.scope.get("route", None)
            if route:
                context["url_mask"] = f"{request.base_url}{route.path[1:]}"

        content = await cls.parse_response(response)

        context["status_code"] = response.status_code
        context["response_data"] = content
        context["elapsed"] = round(datetime.utcnow().timestamp() - context["timestamp"].timestamp(), 4)

        await cls.log(
            event_method=context["level_name"],
            event=f"HTTP Response {context['url']}",
            context=context,
            logger=logger,
        )

        return Response(
            content=content,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    @classmethod
    async def proxy(
        cls,
        request: Request,
        call_next,
        logger: logging.Logger = None,
        *,
        exclude_paths: set[str] = None,
    ) -> Response:
        if exclude_paths:
            matches = [path for path in exclude_paths if re.match(path, str(request.url))]
            if matches:
                return await call_next(request)

        return await cls._proxy(request, call_next, logger)
