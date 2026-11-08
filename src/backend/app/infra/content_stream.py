"""私有内容逐块重验会话与单票撤权；每次使用新的短事务，不持长连接。"""
from __future__ import annotations
import asyncio
import anyio
from app.api.dependencies import set_tenant_context
from app.api.errors import ApiError, NotFoundOrForbidden, ServiceUnavailable
from app.documents.retention import load_case_for_read
from app.identity.service import resolve_session,SESSION_COOKIE
from botocore.exceptions import BotoCoreError, ClientError


async def authorized(request,principal,settings,case_id):
    # PDF下载取消会反复取消同一scope；先完成短事务与归还连接，再传播取消。
    with anyio.CancelScope(shield=True):
        factory=request.app.state.session_factory
        async with factory() as db:
            current=await resolve_session(db,request.cookies.get(SESSION_COOKIE))
            await db.rollback()
            if current is None or current.user_id!=principal.user_id or current.tenant_id!=principal.tenant_id:return False
            try:
                async with db.begin():
                    await set_tenant_context(db,current)
                    await load_case_for_read(db,case_id,user_id=current.user_id,settings=settings)
            except ApiError:return False
    await anyio.lowlevel.checkpoint()
    return True


async def prepared_bytes(request,principal,settings,case_id,*,expected_size=None,**options):
    """发送响应头前读首块；缺对象/存储故障不能先报成功再返回空正文。"""
    stream=guarded_bytes(request,principal,settings,case_id,**options)
    started=False
    async def close():
        await stream.aclose()
        if not started and options.get("close_store") and options.get("store") is not None:
            options["store"].close()
    try:
        if options.get("data") is None and expected_size is not None:
            head=await asyncio.to_thread(options["store"].head,options["key"])
            if head is None:
                raise NotFoundOrForbidden("Document content is unavailable. Please upload the source file again.")
            if head.size_bytes!=expected_size:
                raise ServiceUnavailable("Document content does not match its verified size.")
        started=True
        first=await anext(stream)
    except StopAsyncIteration:
        await close()
        raise NotFoundOrForbidden("Document content is unavailable.") from None
    except ClientError as exc:
        await close()
        if exc.response.get("Error",{}).get("Code") in {"404","NoSuchKey","NotFound"}:
            raise NotFoundOrForbidden("Document content is unavailable. Please upload the source file again.") from None
        raise ServiceUnavailable("Document storage is temporarily unavailable.") from None
    except (BotoCoreError, OSError, RuntimeError) as exc:
        await close()
        if isinstance(exc,ApiError):raise
        raise ServiceUnavailable("Document content could not be read.") from None
    except BaseException:
        await close()
        raise

    async def remaining():
        try:
            # 首块读完到真正发送之间也可能发生撤权。
            if not await authorized(request,principal,settings,case_id):return
            yield first
            async for chunk in stream:yield chunk
        finally:await stream.aclose()
    return remaining()


async def guarded_bytes(request,principal,settings,case_id,*,data=None,store=None,key=None,start=0,end=None,chunk_size=64*1024,close_store=False):
    """首请求已鉴权；取数前及发出每块前再鉴权，撤权后停止后续字节。"""
    size=len(data) if data is not None else end+1
    stop=min(size-1,end) if end is not None else size-1
    position=start
    try:
        while position<=stop:
            if await request.is_disconnected() or not await authorized(request,principal,settings,case_id):return
            boundary=min(stop,position+chunk_size-1)
            chunk=data[position:boundary+1] if data is not None else await asyncio.to_thread(store.read_range,key,position,boundary)
            if not await authorized(request,principal,settings,case_id):return
            if len(chunk)!=boundary-position+1:
                raise RuntimeError("Document content length does not match its verified byte range.")
            yield chunk
            position+=len(chunk)
    finally:
        if store is not None and close_store:
            store.close()
