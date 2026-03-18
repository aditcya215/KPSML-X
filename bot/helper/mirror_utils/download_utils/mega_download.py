#!/usr/bin/env python3
from random import choice as _rand_choice
from secrets import token_hex
from urllib.parse import urlsplit
from aiofiles.os import makedirs
from asyncio import Event
from mega import MegaApi, MegaListener, MegaRequest, MegaTransfer, MegaError

try:
    from mega import MegaProxy
    # Verify both the class constant and the setType instance method exist in
    # this SDK version before claiming the SDK path is usable.
    _ = MegaProxy.PROXY_CUSTOM
    _probe = MegaProxy()
    _probe.setType(MegaProxy.PROXY_CUSTOM)
    del _probe
    _MEGA_PROXY_AVAILABLE = True
except Exception:
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

    def __init__(self, continue_event: Event, listener, suppress_quota_error: bool = False):
        self.continue_event = continue_event
        self.node = None
        self.public_node = None
        self.listener = listener
        self.is_cancelled = False
        self.error = None
        self.__bytes_transferred = 0
        self.__speed = 0
        self.__name = ''
        self.suppress_quota_error = suppress_quota_error
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
        if not self.is_cancelled:
            self.is_cancelled = True
            async_to_sync(self.listener.onDownloadError,
                          f"RequestTempError: {error.toString()}")
        self.error = error.toString()
        self.continue_event.set()

    def onTransferUpdate(self, api: MegaApi, transfer: MegaTransfer):
        if self.is_cancelled:
            api.cancelTransfer(transfer, None)
            self.continue_event.set()
            return
        self.__speed = transfer.getSpeed()
        self.__bytes_transferred = transfer.getTransferredBytes()

    def onTransferFinish(self, api: MegaApi, transfer: MegaTransfer, error):
        try:
            if self.is_cancelled:
                self.continue_event.set()
            elif transfer.isFinished() and (transfer.isFolderTransfer() or transfer.getFileName() == self.__name):
                async_to_sync(self.listener.onDownloadComplete)
                self.continue_event.set()
        except Exception as e:
            LOGGER.error(e)

    def onTransferTemporaryError(self, api, transfer, error):
        filen = transfer.getFileName()
        state = transfer.getState()
        errStr = error.toString()
        LOGGER.error(
            f'Mega download error in file {transfer} {filen}: {error}')
        if state in [1, 4]:
            # Sometimes MEGA (offical client) can't stream a node either and raises a temp failed error.
            # Don't break the transfer queue if transfer's in queued (1) or retrying (4) state [causes seg fault]
            return

        self.error = errStr
        if not self.is_cancelled:
            self.is_cancelled = True
            is_quota = 'quota' in errStr.lower()
            if not (is_quota and self.suppress_quota_error):
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


def _apply_mega_proxy(api, mega_proxy_url):
    """Configure proxy settings on a MegaApi instance.

    Uses MegaProxy SDK bindings when available; falls back to the
    http_proxy / https_proxy environment variables otherwise (libcurl
    honours these at runtime).  Returns True if any proxy was applied.
    """
    if not mega_proxy_url:
        return False
    if _MEGA_PROXY_AVAILABLE:
        proxy = MegaProxy()
        proxy.setType(MegaProxy.PROXY_CUSTOM)
        proxy.setURL(mega_proxy_url)
        api.setProxySettings(proxy)
        return True
    from os import environ as _env
    _env['http_proxy'] = mega_proxy_url
    _env['https_proxy'] = mega_proxy_url
    _env['HTTP_PROXY'] = mega_proxy_url
    _env['HTTPS_PROXY'] = mega_proxy_url
    return True


def _parse_proxy_list(mega_proxy_config):
    """Parse a comma-separated proxy list from the MEGA_PROXY config value.

    Returns a list of stripped, non-empty proxy URL strings.
    """
    if not mega_proxy_config:
        return []
    return [p.strip() for p in mega_proxy_config.split(',') if p.strip()]


def _pick_proxy(proxy_list, used_proxies=None):
    """Pick a random proxy from *proxy_list*, preferring un-used ones.

    Returns an empty string when *proxy_list* is empty (no-proxy mode).
    """
    if not proxy_list:
        return ''
    if used_proxies:
        available = [p for p in proxy_list if p not in used_proxies]
        if available:
            return _rand_choice(available)
    return _rand_choice(proxy_list)


async def add_mega_download(mega_link, path, listener, name):
    MEGA_EMAIL = config_dict['MEGA_EMAIL']
    MEGA_PASSWORD = config_dict['MEGA_PASSWORD']
    MEGA_PROXY_CONFIG = config_dict.get('MEGA_PROXY', '')

    proxy_list = _parse_proxy_list(MEGA_PROXY_CONFIG)
    # When no proxy is configured we still make one attempt (direct connection).
    max_proxy_attempts = max(len(proxy_list), 1)
    used_proxies = set()

    # ── Phase 1: resolve node metadata with the first chosen proxy ──────────
    first_proxy = _pick_proxy(proxy_list, used_proxies)
    if first_proxy:
        used_proxies.add(first_proxy)

    executor = AsyncExecutor()
    api = MegaApi(None, None, None, 'KPSML-X')
    folder_api = None

    try:
        if first_proxy and _apply_mega_proxy(api, first_proxy):
            _parts = urlsplit(first_proxy)
            LOGGER.info(
                f"MEGA proxy mode enabled: "
                f"{_parts.scheme or '?'}://{_parts.hostname or '?'}:{_parts.port or '?'}"
            )

        # suppress_quota_error=True when there are more proxies to try so that
        # the first quota failure doesn't terminate the task immediately.
        mega_listener = MegaAppListener(
            executor.continue_event, listener,
            suppress_quota_error=(max_proxy_attempts > 1),
        )
        api.addListener(mega_listener)

        if MEGA_EMAIL and MEGA_PASSWORD:
            await executor.do(api.login, (MEGA_EMAIL, MEGA_PASSWORD))

        if get_mega_link_type(mega_link) == "file":
            await executor.do(api.getPublicNode, (mega_link,))
            node = mega_listener.public_node
        else:
            folder_api = MegaApi(None, None, None, 'KPSML-X')
            if first_proxy:
                _apply_mega_proxy(folder_api, first_proxy)
            folder_api.addListener(mega_listener)
            await executor.do(folder_api.loginToFolder, (mega_link,))
            node = await sync_to_async(folder_api.authorizeNode, mega_listener.node)
        if mega_listener.error is not None:
            await sendMessage(listener.message, str(mega_listener.error))
            await executor.do(api.logout, ())
            if folder_api is not None:
                await executor.do(folder_api.logout, ())
            return
        if node is None:
            await sendMessage(listener.message, "Failed to retrieve MEGA node. The link may be invalid or expired.")
            await executor.do(api.logout, ())
            if folder_api is not None:
                await executor.do(folder_api.logout, ())
            return

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
            LOGGER.info(f'Added to Queue/Download: {name}')
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

        if from_queue:
            LOGGER.info(f'Start Queued Download from Mega: {name}')
        else:
            await listener.onDownloadStart()
            await sendStatusMessage(listener.message)
            LOGGER.info(f'Download from Mega: {name}')

        await makedirs(path, exist_ok=True)

        # ── Phase 2: proxy-rotation retry loop for the actual download ───────
        # attempt=0 reuses the api/node already set up above.
        # Subsequent attempts create fresh API instances with a new proxy.
        dl_api, dl_folder_api, dl_executor, dl_listener, dl_node = (
            api, folder_api, executor, mega_listener, node
        )

        for attempt in range(max_proxy_attempts):
            is_last_attempt = (attempt == max_proxy_attempts - 1)

            if attempt > 0:
                # Quota exceeded on the previous attempt – pick a fresh proxy.
                cur_proxy = _pick_proxy(proxy_list, used_proxies)
                if cur_proxy:
                    used_proxies.add(cur_proxy)
                LOGGER.warning(
                    f"MEGA quota exceeded; retrying with "
                    f"{cur_proxy or 'direct connection'} "
                    f"(attempt {attempt + 1}/{max_proxy_attempts})"
                )

                dl_executor = AsyncExecutor()
                dl_api = MegaApi(None, None, None, 'KPSML-X')
                dl_folder_api = None

                if cur_proxy:
                    _apply_mega_proxy(dl_api, cur_proxy)
                    _parts = urlsplit(cur_proxy)
                    LOGGER.info(
                        f"MEGA proxy mode enabled: "
                        f"{_parts.scheme or '?'}://{_parts.hostname or '?'}:{_parts.port or '?'}"
                    )

                dl_listener = MegaAppListener(
                    dl_executor.continue_event, listener,
                    suppress_quota_error=not is_last_attempt,
                )
                dl_api.addListener(dl_listener)

                if MEGA_EMAIL and MEGA_PASSWORD:
                    await dl_executor.do(dl_api.login, (MEGA_EMAIL, MEGA_PASSWORD))

                if get_mega_link_type(mega_link) == "file":
                    await dl_executor.do(dl_api.getPublicNode, (mega_link,))
                    dl_node = dl_listener.public_node
                else:
                    dl_folder_api = MegaApi(None, None, None, 'KPSML-X')
                    if cur_proxy:
                        _apply_mega_proxy(dl_folder_api, cur_proxy)
                    dl_folder_api.addListener(dl_listener)
                    await dl_executor.do(dl_folder_api.loginToFolder, (mega_link,))
                    dl_node = await sync_to_async(
                        dl_folder_api.authorizeNode, dl_listener.node
                    )

                if dl_listener.error is not None:
                    LOGGER.error(
                        f"MEGA node fetch failed on attempt {attempt + 1}: "
                        f"{dl_listener.error}"
                    )
                    await dl_executor.do(dl_api.logout, ())
                    if dl_folder_api is not None:
                        await dl_executor.do(dl_folder_api.logout, ())
                    if is_last_attempt:
                        await listener.onDownloadError(str(dl_listener.error))
                    continue

                if dl_node is None:
                    LOGGER.error(
                        f"Could not retrieve MEGA node on attempt {attempt + 1}"
                    )
                    await dl_executor.do(dl_api.logout, ())
                    if dl_folder_api is not None:
                        await dl_executor.do(dl_folder_api.logout, ())
                    if is_last_attempt:
                        await listener.onDownloadError(
                            "Failed to retrieve MEGA node."
                        )
                    continue

            # Update download_dict so the status widget reflects the current
            # listener (speed / bytes come from the active dl_listener).
            async with download_dict_lock:
                download_dict[listener.uid] = MegaDownloadStatus(
                    name, size, gid, dl_listener,
                    listener.message, listener.upload_details,
                )
            if attempt == 0:
                async with queue_dict_lock:
                    non_queued_dl.add(listener.uid)

            await dl_executor.do(
                dl_api.startDownload, (dl_node, path, name, None, False, None)
            )
            await dl_executor.do(dl_api.logout, ())
            if dl_folder_api is not None:
                await dl_executor.do(dl_folder_api.logout, ())

            # Retry if quota was exceeded and we still have proxies to try.
            if (
                not is_last_attempt
                and dl_listener.error
                and 'quota' in str(dl_listener.error).lower()
            ):
                continue

            # Success or unrecoverable error on the last attempt – stop.
            break

    except Exception as e:
        LOGGER.error(f'Mega download unexpected error: {e}', exc_info=True)
        await listener.onDownloadError(f"MEGA download failed: {e}")
