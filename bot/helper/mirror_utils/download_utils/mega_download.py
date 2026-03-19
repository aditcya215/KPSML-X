#!/usr/bin/env python3
from random import choice as random_choice
from secrets import token_hex
from aiofiles.os import makedirs
from asyncio import Event, sleep, create_task
from mega import MegaApi, MegaListener, MegaRequest, MegaTransfer, MegaError

try:
    from mega import MegaProxy
    _MEGA_PROXY_AVAILABLE = True
except ImportError:
    _MEGA_PROXY_AVAILABLE = False

from bot import LOGGER, config_dict, download_dict_lock, download_dict, non_queued_dl, queue_dict_lock
from bot.helper.telegram_helper.message_utils import sendMessage, sendStatusMessage
from bot.helper.ext_utils.bot_utils import get_mega_link_type, async_to_sync, sync_to_async
from bot.helper.mirror_utils.status_utils.mega_download_status import MegaDownloadStatus
from bot.helper.mirror_utils.status_utils.queue_status import QueueStatus
from bot.helper.ext_utils.task_manager import is_queued, limit_checker, stop_duplicate_check


class MegaAppListener(MegaListener):
    _NO_EVENT_ON = (MegaRequest.TYPE_LOGIN, MegaRequest.TYPE_FETCH_NODES)
    NO_ERROR = "no error"

    def __init__(self, continue_event: Event, listener):
        self.continue_event = continue_event
        self.node = None
        self.public_node = None
        self.listener = listener
        self.is_cancelled = False
        self.is_quota_error = False
        self.is_stalled = False
        self.error = None
        self.__bytes_transferred = 0
        self.__speed = 0
        self.__name = ''
        # Generation counter: incremented on each proxy-rotation retry so that
        # callbacks belonging to a stale/cancelled transfer are silently dropped.
        self._dl_gen = 0
        super().__init__()

    @property
    def speed(self):
        return self.__speed

    @property
    def downloaded_bytes(self):
        return self.__bytes_transferred

    def onRequestFinish(self, api, request, error):
        if str(error).lower() != "no error":
            self.error = error.copy()
            LOGGER.error(f'Mega onRequestFinishError: {self.error}')
            self.continue_event.set()
            return
        request_type = request.getType()
        if request_type == MegaRequest.TYPE_LOGIN:
            api.fetchNodes()
        elif request_type == MegaRequest.TYPE_GET_PUBLIC_NODE:
            self.public_node = request.getPublicMegaNode()
            self.__name = self.public_node.getName()
        elif request_type == MegaRequest.TYPE_FETCH_NODES:
            LOGGER.info("Fetching Root Node.")
            self.node = api.getRootNode()
            self.__name = self.node.getName()
            LOGGER.info(f"Node Name: {self.node.getName()}")
        if request_type not in self._NO_EVENT_ON or self.node and "cloud drive" not in self.__name.lower():
            self.continue_event.set()

    def onRequestTemporaryError(self, api, request, error: MegaError):
        LOGGER.error(f'Mega Request error in {error}')
        # Temporary error; MEGA SDK retries the request internally.
        # onRequestFinish (above) will be called with the final outcome – do NOT
        # cancel the download or set the continue_event here.

    def onTransferUpdate(self, api: MegaApi, transfer: MegaTransfer):
        if self.is_cancelled:
            api.cancelTransfer(transfer, None)
            self.continue_event.set()
            return
        self.__speed = transfer.getSpeed()
        self.__bytes_transferred = transfer.getTransferredBytes()

    def onTransferFinish(self, api: MegaApi, transfer: MegaTransfer, error):
        gen = self._dl_gen
        try:
            if self.is_cancelled:
                self.continue_event.set()
            elif (
                gen == self._dl_gen
                and transfer.isFinished()
                and (transfer.isFolderTransfer() or transfer.getFileName() == self.__name)
            ):
                async_to_sync(self.listener.onDownloadComplete)
                self.continue_event.set()
        except Exception as e:
            LOGGER.error(e)

    def onTransferTemporaryError(self, api, transfer, error):
        gen = self._dl_gen
        filen = transfer.getFileName()
        state = transfer.getState()
        errStr = error.toString()
        if state in [1, 4]:
            # Sometimes MEGA (offical client) can't stream a node either and raises a temp failed error.
            # Don't break the transfer queue if transfer's in queued (1) or retrying (4) state [causes seg fault]
            return
        if self.is_cancelled:
            # Already handled; suppress duplicate error logs for the same transfer failure.
            return
        if gen != self._dl_gen:
            # Stale callback from a superseded transfer; drop it.
            return

        LOGGER.error(
            f'Mega download error in file {transfer} {filen}: {error}')
        self.error = errStr
        self.is_cancelled = True
        if 'quota' in errStr.lower():
            self.is_quota_error = True
            self.continue_event.set()
        else:
            async_to_sync(self.listener.onDownloadError,
                          f"TransferTempError: {errStr} ({filen})")
            self.continue_event.set()

    async def cancel_download(self):
        self.is_cancelled = True
        await self.listener.onDownloadError("Download Canceled by user")


class AsyncExecutor:

    def __init__(self):
        self.continue_event = Event()

    async def do(self, function, args):
        self.continue_event.clear()
        await sync_to_async(function, *args)
        await self.continue_event.wait()


# Stall detection: rotate proxy when download makes no progress for this long.
_STALL_TIMEOUT = 60    # seconds of zero progress before treating as a stall
_STALL_CHECK   = 10    # polling interval in seconds


async def _stall_watchdog(mega_listener, api, timeout=_STALL_TIMEOUT, interval=_STALL_CHECK):
    """Detect mid-transfer proxy bandwidth stalls and trigger proxy rotation.

    When the proxy hits its bandwidth cap the TCP connection stays open but no
    data flows.  The MEGA SDK never fires a quota error in this case – it just
    waits indefinitely.  This watchdog polls `downloaded_bytes` every
    `interval` seconds and, if there has been no progress for `timeout`
    seconds, marks the transfer as a quota/stall error so the outer loop can
    rotate to the next proxy.
    """
    last_bytes = None
    stall_elapsed = 0
    while not mega_listener.is_cancelled:
        await sleep(interval)
        if mega_listener.is_cancelled:
            break
        current_bytes = mega_listener.downloaded_bytes
        if last_bytes is None:
            # First observation – just record the baseline.
            last_bytes = current_bytes
            continue
        if current_bytes == last_bytes:
            stall_elapsed += interval
            if stall_elapsed >= timeout:
                LOGGER.warning(
                    f"MEGA download stalled for {stall_elapsed}s "
                    f"(proxy bandwidth limit?), triggering proxy rotation"
                )
                mega_listener.is_stalled = True
                mega_listener.is_quota_error = True
                mega_listener.is_cancelled = True
                # Immediately unblock the executor.  onTransferUpdate also sets
                # is_cancelled=True and calls api.cancelTransfer on its next
                # invocation (MEGA SDK timer), but we don't wait for that.
                mega_listener.continue_event.set()
                break
        else:
            stall_elapsed = 0
            last_bytes = current_bytes


def _apply_mega_proxy(api, mega_proxy_url):
    """Configure proxy settings on a MegaApi instance.

    Uses MegaProxy SDK bindings when available; falls back to the
    http_proxy / https_proxy environment variables otherwise (libcurl
    honours these at runtime).  Returns True if any proxy was applied.
    """
    if not mega_proxy_url:
        return False
    if _MEGA_PROXY_AVAILABLE:
        try:
            proxy = MegaProxy()
            if hasattr(proxy, 'setType') and hasattr(MegaProxy, 'PROXY_CUSTOM'):
                proxy.setType(MegaProxy.PROXY_CUSTOM)
            if hasattr(proxy, 'setURL'):
                proxy.setURL(mega_proxy_url)
                api.setProxySettings(proxy)
                return True
            # setURL not available in this SDK build; fall through to env vars.
        except Exception as e:
            LOGGER.warning(
                f"MegaProxy SDK error ({e}), falling back to environment variables for {mega_proxy_url}"
            )
    from os import environ as _env
    _env['http_proxy'] = mega_proxy_url
    _env['https_proxy'] = mega_proxy_url
    _env['HTTP_PROXY'] = mega_proxy_url
    _env['HTTPS_PROXY'] = mega_proxy_url
    return True


async def add_mega_download(mega_link, path, listener, name):
    MEGA_EMAIL = config_dict['MEGA_EMAIL']
    MEGA_PASSWORD = config_dict['MEGA_PASSWORD']
    MEGA_PROXY_RAW = config_dict.get('MEGA_PROXY', '')

    # Support comma-separated list of proxies; each is tried in turn on quota errors.
    proxy_list = (
        [p.strip() for p in MEGA_PROXY_RAW.split(',') if p.strip()]
        if MEGA_PROXY_RAW.strip() else []
    )
    tried_proxies: set = set()
    current_proxy = random_choice(proxy_list) if proxy_list else ''

    executor = AsyncExecutor()
    api = MegaApi(None, None, None, 'KPSML-X')
    folder_api = None

    # --- Login / fetch-nodes phase with per-proxy retry ---
    while True:
        if current_proxy and _apply_mega_proxy(api, current_proxy):
            LOGGER.info(f"MEGA proxy mode enabled: {current_proxy}")

        mega_listener = MegaAppListener(executor.continue_event, listener)
        api.addListener(mega_listener)

        if MEGA_EMAIL and MEGA_PASSWORD:
            await executor.do(api.login, (MEGA_EMAIL, MEGA_PASSWORD))

        if get_mega_link_type(mega_link) == "file":
            await executor.do(api.getPublicNode, (mega_link,))
            node = mega_listener.public_node
        else:
            folder_api = MegaApi(None, None, None, 'KPSML-X')
            if current_proxy:
                _apply_mega_proxy(folder_api, current_proxy)
            folder_api.addListener(mega_listener)
            await executor.do(folder_api.loginToFolder, (mega_link,))
            # Guard against authorizeNode(None) when login failed.
            if mega_listener.error is None and mega_listener.node is not None:
                node = await sync_to_async(folder_api.authorizeNode, mega_listener.node)
            else:
                node = None

        if mega_listener.error is not None:
            # Login/fetch failed – try the next proxy if one is available.
            if current_proxy:
                tried_proxies.add(current_proxy)
            remaining = [p for p in proxy_list if p not in tried_proxies]
            if remaining:
                current_proxy = random_choice(remaining)
                LOGGER.warning(
                    f"Mega login failed ({mega_listener.error}), "
                    f"rotating to next proxy: {current_proxy}"
                )
                # Clean up the failed session before retrying.
                await executor.do(api.logout, ())
                api.removeListener(mega_listener)
                if folder_api is not None:
                    await executor.do(folder_api.logout, ())
                    folder_api.removeListener(mega_listener)
                    folder_api = None
                # Fresh executor / API object for the next attempt.
                executor = AsyncExecutor()
                api = MegaApi(None, None, None, 'KPSML-X')
                continue

            # All proxies exhausted – report the error and bail out.
            await sendMessage(listener.message, str(mega_listener.error))
            await executor.do(api.logout, ())
            if folder_api is not None:
                await executor.do(folder_api.logout, ())
            return

        break  # Login / fetch succeeded

    name = name or node.getName()
    msg, button = await stop_duplicate_check(name, listener)
    if msg:
        await sendMessage(listener.message, msg, button)
        await executor.do(api.logout, ())
        if folder_api is not None:
            await executor.do(folder_api.logout, ())
        return

    gid = token_hex(5)
    size = api.getSize(node)
    if limit_exceeded := await limit_checker(size, listener, isMega=True):
        await sendMessage(listener.message, limit_exceeded)
        return
    added_to_queue, event = await is_queued(listener.uid)
    if added_to_queue:
        LOGGER.info(f"Added to Queue/Download: {name}")
        async with download_dict_lock:
            download_dict[listener.uid] = QueueStatus(
                name, size, gid, listener, 'Dl')
        await listener.onDownloadStart()
        await sendStatusMessage(listener.message)
        await event.wait()
        async with download_dict_lock:
            if listener.uid not in download_dict:
                await executor.do(api.logout, ())
                if folder_api is not None:
                    await executor.do(folder_api.logout, ())
                return
        from_queue = True
        LOGGER.info(f'Start Queued Download from Mega: {name}')
    else:
        from_queue = False

    async with download_dict_lock:
        download_dict[listener.uid] = MegaDownloadStatus(name, size, gid, mega_listener, listener.message, listener.upload_details)
    async with queue_dict_lock:
        non_queued_dl.add(listener.uid)

    if from_queue:
        LOGGER.info(f'Start Queued Download from Mega: {name}')
    else:
        await listener.onDownloadStart()
        await sendStatusMessage(listener.message)
        LOGGER.info(f"Download from Mega: {name}")

    await makedirs(path, exist_ok=True)

    while True:
        # Safety check: abort if the task was externally removed (e.g. user cancel).
        async with download_dict_lock:
            if listener.uid not in download_dict:
                await executor.do(api.logout, ())
                if folder_api is not None:
                    await executor.do(folder_api.logout, ())
                return

        # Reset listener state for this download attempt.
        mega_listener.is_cancelled = False
        mega_listener.is_quota_error = False
        mega_listener.is_stalled = False
        mega_listener.error = None

        # Run the stall watchdog concurrently with the download so that a
        # proxy bandwidth exhaustion (which stalls silently) is detected and
        # handled via the same proxy-rotation path as quota errors.
        watchdog = create_task(_stall_watchdog(mega_listener, api))
        try:
            await executor.do(api.startDownload, (node, path, name, None, False, None))
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except Exception:
                pass

        if mega_listener.is_quota_error:
            if current_proxy:
                tried_proxies.add(current_proxy)
            remaining = [p for p in proxy_list if p not in tried_proxies]
            if remaining:
                current_proxy = random_choice(remaining)
                reason = "stalled (proxy bandwidth limit?)" if mega_listener.is_stalled else "quota exceeded"
                LOGGER.warning(
                    f"MEGA transfer {reason}, rotating to new proxy: {current_proxy}"
                )
                _apply_mega_proxy(api, current_proxy)
                if folder_api is not None:
                    _apply_mega_proxy(folder_api, current_proxy)
                # Increment the generation counter so that any lingering
                # callbacks from the cancelled transfer are silently ignored.
                mega_listener._dl_gen += 1
                continue
            LOGGER.error("Quota exceeded / stalled on all available proxies")
            await listener.onDownloadError("Mega download failed: Over quota or stalled on all proxies")
            await executor.do(api.logout, ())
            if folder_api is not None:
                await executor.do(folder_api.logout, ())
            return

        break

    await executor.do(api.logout, ())
    if folder_api is not None:
        await executor.do(folder_api.logout, ())
