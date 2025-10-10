"""
This file contains the BotManager class, which manages the bot logic, game state, and automation
It also manages the browser and overlay display
The BotManager class is run in a separate thread, and provide interface methods for UI
"""
# pylint: disable=broad-exception-caught
import time
import queue
import threading
import json

from game.browser import GameBrowser
from game.MahjongGameState import MahjongGameState
from game.automation import Automation, UiState, JOIN_GAME, END_GAME
import mitm
import proxinject
import liqi
from common.mj_helper import MjaiType, GameInfo, MJAI_TILE_2_UNICODE, ActionUnicode, MJAI_TILES_34, MJAI_AKA_DORAS
from common.log_helper import LOGGER
from common.settings import Settings
from common.lan_str import LanStr
from common import utils
from common.utils import FPSCounter
from bot import Bot, get_bot


METHODS_TO_IGNORE = [
    liqi.LiqiMethod.checkNetworkDelay,
    liqi.LiqiMethod.heartbeat,
    liqi.LiqiMethod.routeHeartbeat,
    liqi.LiqiMethod.loginBeat,
    liqi.LiqiMethod.fetchAccountActivityData,
    liqi.LiqiMethod.fetchServerTime,
    liqi.LiqiMethod.oauth2Login,
]

class BotManager:
    """ Bot logic manager"""
    def __init__(self, setting:Settings) -> None:
        self.st = setting
        self.game_state:MahjongGameState = None

        self.liqi_parser = liqi.LiqiProto()
        self.mitm_server:mitm.MitmController = mitm.MitmController()      # no domain restrictions for now
        self.proxy_injector = proxinject.ProxyInjector()
        self.browser = GameBrowser(self.st.browser_width, self.st.browser_height)
        self.automation = Automation(self.browser, self.st)
        self.bot:Bot = None

        self._thread:threading.Thread = None
        self._stop_event = threading.Event()
        self.fps_counter = FPSCounter()

        self.lobby_flow_id:str = None                   # websocket flow Id for lobby
        self.game_flow_id = None                        # websocket flow that corresponds to the game/match
       
        self.bot_need_update:bool = True                # set this True to update bot in main thread
        self.mitm_proxinject_need_update:bool = False    # set this True to update mitm and prox inject in main thread
        self.is_loading_bot:bool = False                # is bot being loaded
        self.main_thread_exception:Exception = None     # Exception that had stopped the main thread
        self.game_exception:Exception = None            # game run time error (but does not break main thread)        
        self._amulet_active: bool = False
        self._amulet_info: dict = {
            "stage": 0,
            "hands": [],
            "desktop_remain": 0,
            "ended": False,
        }
        self._amulet_pending_action: dict | None = None
        self._amulet_pool: list[dict] | None = None  # 108张：[{"id":72,"tile":"9m"}, ...]
        self._amulet_draw_ids: list[int] = []  # 中段36张的牌 id（按顺序）
        self._amulet_used_ids: set[int] = set()  # usedDesktop 累积的牌 id
        self._amulet_desktop_remain: int = 0  # desktopRemain（服务器口径）
        self._amulet_replace_ids: list[int] = []  # 尾部49张：用于换牌阶段的待替换队列（按顺序）
        self._amulet_replace_cursor: int = 0      # 已消耗（已从队列取走）的数量

    def start(self):
        """ Start bot manager thread"""
        self._thread = threading.Thread(
            target=self._run,
            name="BotThread",
            daemon=True
        )
        self._thread.start()


    def stop(self, join_thread:bool):
        """ Stop bot manager thread"""
        self._stop_event.set()
        if join_thread:
            self._thread.join()


    def is_running(self) -> bool:
        """ return True if bot manager thread is running"""
        if self._thread and self._thread.is_alive():
            return True
        else:
            return False


    def is_in_game(self) -> bool:
        """ return True if the bot is currently in a game """
        if self.game_state:
            return True
        else:
            return False


    def get_game_info(self) -> GameInfo:
        """ Get gameinfo derived from game_state. can be None"""
        if self.game_state is None:
            return None

        return self.game_state.get_game_info()


    def is_game_syncing(self) -> bool:
        """ is mjai syncing game messages (from disconnection) """
        if self.game_state:
            return self.game_state.is_ms_syncing


    def get_game_error(self) -> Exception:
        """ return game error msg if any, or none if not
        These are errors that do not break the main thread, but main impact individual games
        e.g. game state error / ai bot error
        """
        return self.game_exception


    def get_game_client_type(self) -> utils.GameClientType:
        """ return the running game client type. return None if none is running"""
        if self.browser.is_running():
            return utils.GameClientType.PLAYWRIGHT
        elif self.lobby_flow_id or self.game_flow_id:
            return utils.GameClientType.PROXY
        else:
            return None

    def start_browser(self):
        """ Start the browser thread, open browser window """
        ms_url = self.st.ms_url
        proxy = self.mitm_server.proxy_str
        self.browser.start(ms_url, proxy, self.st.browser_width, self.st.browser_height, self.st.enable_chrome_ext)

    def is_browser_zoom_off(self):
        """ check browser zoom level, return true if zoomlevel is not 1"""
        if self.browser and self.browser.is_page_normal():
            zoom = self.browser.zoomlevel_check
            if zoom is not None:
                if abs(zoom - 1) > 0.001:
                    return True
        return False

    # mitm restart not working for now. disable this.
    # def set_mitm_proxinject_update(self):
    #     """ restart mitm proxy server"""
    #     self.mitm_proxinject_need_update = True


    def set_bot_update(self):
        """ mark bot needs update"""
        self.bot_need_update = True


    def is_bot_created(self):
        """ return true if self.bot is not None"""
        return self.bot is not None


    def is_bot_calculating(self):
        """ return true if bot is calculating"""
        if self.game_state and self.game_state.is_bot_calculating:
            return True
        else:
            return False


    def get_pending_reaction(self) -> dict:
        """ returns the pending mjai output reaction (which hasn't been acted on)"""
        if self.game_state:
            reaction = self.game_state.get_pending_reaction()
            return reaction
        else:   # None
            return None


    def enable_overlay(self):
        """ Start the overlay thread"""
        LOGGER.debug("Bot Manager enabling overlay")
        self.st.enable_overlay = True


    def disable_overlay(self):
        """ disable browser overlay"""
        LOGGER.debug("Bot Manager disabling overlay")
        self.st.enable_overlay = False


    def update_overlay(self):
        """ update the overlay if conditions are met"""
        if self._update_overlay_conditions_met():
            self._update_overlay_guide()
            self._update_overlay_botleft()


    def enable_automation(self):
        """ enable automation"""
        LOGGER.debug("Bot Manager enabling automation")
        self.st.enable_automation = True
        self.automation.decide_lobby_action()


    def disable_automation(self):
        """ disable automation"""
        LOGGER.debug("Bot Manager disabling automation")
        self.st.enable_automation = False
        self.automation.stop_previous()


    def enable_autojoin(self):
        """ enable autojoin"""
        LOGGER.debug("Enabling Auto Join")
        self.st.auto_join_game = True


    def disable_autojoin(self):
        """ disable autojoin"""
        LOGGER.debug("Disabling Auto Join")
        self.st.auto_join_game = False
        # stop any lobby tasks
        if self.automation.is_running_execution():
            name, _d = self.automation.running_task_info()
            if name in (JOIN_GAME, END_GAME):
                self.automation.stop_previous()

    def _create_bot(self):
        """ create Bot object based on settings"""
        try:
            self.is_loading_bot = True
            self.bot = None
            self.bot = get_bot(self.st)
            self.game_exception = None
            LOGGER.info("Created bot: %s. Supported Modes: %s", self.bot.name, self.bot.supported_modes)
        except Exception as e:
            LOGGER.warning("Failed to create bot: %s", e, exc_info=True)
            self.bot = None
            self.game_exception = e
        self.is_loading_bot = False

    def _create_mitm_and_proxinject(self):
        # create mitm and proxinject threads
        # enable proxyinject requires socks5, which disables upstream proxy
        if self.st.enable_proxinject:
            mode = mitm.SOCKS5
            LOGGER.debug("Enabling proxyinject requires socks5, and it disables upstream proxy")
        else:
            mode = mitm.HTTP

        self.mitm_server.start(self.st.mitm_port, mode, self.st.upstream_proxy)
        res = self.mitm_server.install_mitm_cert()
        if not res:
            self.main_thread_exception = utils.MitmCertNotInstalled(self.mitm_server.cert_file)

        if self.st.enable_proxinject:
            self.proxy_injector.start(self.st.inject_process_name, "127.0.0.1", self.st.mitm_port)


    def _run(self):
        """ Keep running the main loop (blocking)"""
        try:
            self._create_mitm_and_proxinject()
            if self.st.auto_launch_browser:
                self.start_browser()

            while self._stop_event.is_set() is False:   # thread main loop
                # keep processing majsoul game messages forwarded from mitm server
                self.fps_counter.frame()
                self._loop_pre_msg()
                try:
                    msg = self.mitm_server.get_message()
                    self._process_msg(msg)
                except queue.Empty:
                    time.sleep(0.002)
                except Exception as e:
                    LOGGER.error("Error processing msg: %s",e, exc_info=True)
                    self.game_exception = e
                self._loop_post_msg()

            # loop ended, clean up before exit
            LOGGER.info("Shutting down browser")
            self.browser.stop(True)
            LOGGER.info("Shutting down MITM")
            self.mitm_server.stop()
            if self.proxy_injector.is_running():
                LOGGER.info("Shutting down proxy injector")
                self.proxy_injector.stop(True)
            LOGGER.info("Bot manager thread ending.")

        except Exception as e:
            self.main_thread_exception = e
            LOGGER.error("Bot Manager Thread Exception: %s", e, exc_info=True)


    def _loop_pre_msg(self):
        """ things to do every loop before processing msg"""
        #  update bot if needed
        if self.bot_need_update and self.is_in_game() is False:
            self._create_bot()
            self.bot_need_update = False

        # update mitm if needed: when no one is using mitm
        if self.mitm_proxinject_need_update:
            if not (self.browser.is_running()):
                LOGGER.debug("Updating mitm and proxy injector")
                self.proxy_injector.stop(True)
                self.mitm_server.stop()
                self._create_mitm_and_proxinject()
                self.mitm_proxinject_need_update = False


    def _loop_post_msg(self):
        # things to do in every loop after processing msg
        # check mitm
        if self.mitm_server.is_running() is False:
            self.game_exception = utils.MITMException("MITM server stopped")
        else:   # clear exception
            if isinstance(self.game_exception, utils.MITMException):
                self.game_exception = None

        # check overlay
        if self.browser and self.browser.is_page_normal():
            if self.st.enable_overlay:
                if self.browser.is_overlay_working() is False:
                    LOGGER.debug("Bot manager attempting turning on browser overlay")
                    self.browser.start_overlay()
                    # self._update_overlay_guide()
            else:
                if self.browser.is_overlay_working():
                    LOGGER.debug("Bot manager turning off browser overlay")
                    self.browser.stop_overlay()

        self.automation.automate_retry_pending(self.game_state)            # retry failed automation

        if not self.game_exception:     # skip on game error
            self.automation.decide_lobby_action()

    def _amulet_on_fetch_data(self, liqi_data: dict) -> None:
        game = (liqi_data.get('data') or {}).get('game')
        if not game:
            return
        round_obj = game.get('round') or {}
        pool = round_obj.get('pool') or []
        if pool:
            self._amulet_set_pool_from_array(pool)
        # 初始化 GUI 可见的 amulet 简要信息（stage/hands/desktop_remain/ended）
        try:
            game_obj = liqi_data.get('data', {}).get('game', {})
            round_obj2 = (game_obj.get('round') or {})
            # stage / ended
            self._amulet_info['stage'] = int(game_obj.get('stage') or 0)
            self._amulet_info['ended'] = bool(game_obj.get('ended') or False)
            # hands
            hands_init = round_obj2.get('hands') or []
            if isinstance(hands_init, list):
                self._amulet_info['hands'] = list(hands_init)
            # desktopRemain
            if 'desktopRemain' in round_obj2:
                dr = int(round_obj2.get('desktopRemain') or 0)
                self._amulet_info['desktop_remain'] = dr
        except Exception:
            pass
        # 采纳旧局的 usedDesktop
        used = set(round_obj.get('usedDesktop') or [])
        self._amulet_used_ids = {int(x) for x in used}
        # 同步服务器剩余
        self._amulet_desktop_remain = int(round_obj.get('desktopRemain') or 0)
        # 若旧局里已有换牌记录，used 为累计替换掉的牌（长度即为游标）
        used_replace = round_obj.get('used') or []
        try:
            if isinstance(used_replace, list):
                self._amulet_replace_cursor = len(used_replace)
        except Exception:
            pass

    def _amulet_on_upgrade_events(self, events: list[dict]) -> None:
        """amuletActivityUpgrade：events 里某个 event 的 valueChanges.round.pool.value 是 108 张对象数组"""
        for ev in events:
            vc = ev.get("valueChanges") or {}
            rd = vc.get("round") or {}
            pool = rd.get("pool") or {}
            if isinstance(pool, dict) and pool.get("dirty") and isinstance(pool.get("value"), list):
                self._amulet_set_pool_from_array(pool["value"])
            # 也顺手拿 desktopRemain
            dr = rd.get("desktopRemain")
            if isinstance(dr, dict) and dr.get("dirty"):
                self._amulet_desktop_remain = int(dr.get("value") or 0)
            used_rep = rd.get("used")
            if isinstance(used_rep, dict) and used_rep.get("dirty"):
                vals = used_rep.get("value") or []
                try:
                    self._amulet_replace_cursor = len(vals)
                except Exception:
                    pass
        # 同步 GUI 简要信息
        try:
            self._amulet_update_from_events(events)
        except Exception:
            pass

    def _amulet_on_operate_events(self, events: list[dict]) -> None:
        """amuletActivityOperate：刷新 usedDesktop / hands / desktopRemain 等"""
        for ev in events:
            vc = ev.get("valueChanges") or {}
            rd = vc.get("round") or {}
            # usedDesktop 是“已摸出”的牌 id（来自中段36张）
            used = rd.get("usedDesktop")
            if isinstance(used, dict) and used.get("dirty"):
                vals = used.get("value") or []
                # 服务端给的是“当前所有已用”的集合，直接覆盖更稳妥
                self._amulet_used_ids = set(int(x) for x in vals)

            # 桌面剩余
            dr = rd.get("desktopRemain")
            if isinstance(dr, dict) and dr.get("dirty"):
                self._amulet_desktop_remain = int(dr.get("value") or 0)

            # 换牌阶段的 used（累计被替换掉的手牌 id）——长度即游标
            used_rep = rd.get("used")
            if isinstance(used_rep, dict) and used_rep.get("dirty"):
                vals = used_rep.get("value") or []
                try:
                    self._amulet_replace_cursor = len(vals)
                except Exception:
                    pass
            # hands 你若想显示也可以存，但不影响“可摸剩余”的计算
            # hands = rd.get("hands")
            # if isinstance(hands, dict) and hands.get("dirty"):
            #     self._amulet_hands_ids = list(hands.get("value") or [])
        # 同步 GUI 简要信息
        try:
            self._amulet_update_from_events(events)
        except Exception:
            pass

    def _amulet_set_pool_from_array(self, pool: list[dict]) -> None:
        """设置 108 张牌山，并切出中段36张（可摸段）与尾部49张（换牌队列）的 id 顺序"""
        self._amulet_pool = pool[:]  # 浅拷贝
        if len(self._amulet_pool) != 108:
            LOGGER.warning("Amulet pool length unexpected: %s", len(self._amulet_pool))
        # 可摸段：索引 23~58（切片上界 59）
        draw_start, draw_end = 23, 59
        draw_slice = self._amulet_pool[draw_start:draw_end]
        self._amulet_draw_ids = [int(x.get("id")) for x in draw_slice if isinstance(x, dict) and "id" in x]
        # 换牌队列：索引 59~107（切片上界 108）
        rep_start, rep_end = 59, 108
        rep_slice = self._amulet_pool[rep_start:rep_end]
        self._amulet_replace_ids = [int(x.get("id")) for x in rep_slice if isinstance(x, dict) and "id" in x]
        # 重置 used 集合与游标
        self._amulet_used_ids = set()
        self._amulet_desktop_remain = 36
        self._amulet_replace_cursor = 0

    # --- 提供给 GUI 的文本构造 ---

    def get_amulet_drawable_text(self) -> str:
        """
        显示青云之志“可摸段”(36张)里 **剩余的 N 张**：
          - N = desktopRemain（来自服务端）
          - 取 36 段的**尾部 N 张**（前面被摸走的在前面，尾部才是“还没到手”的）
          - 分行显示：每行 9 张；第一行显示 N % 9 张（若余数为 0 则每行刚好 9 张）
          - 不再使用 usedDesktop 进行空洞占位
        """
        if not (self._amulet_active and self._amulet_pool and self._amulet_draw_ids):
            return ""

        # id -> tile 映射
        id2tile: dict[int, str] = {}
        for obj in self._amulet_pool:
            try:
                id2tile[int(obj["id"])] = str(obj["tile"])
            except Exception:
                continue

        # 剩余数量（夹在 0..36）
        remain = max(0, min(int(self._amulet_desktop_remain or 0), len(self._amulet_draw_ids)))
        if remain == 0:
            header = "[可摸剩余 0/36]"
            return header

        # 取“可摸段”尾部 N 张
        remain_ids = self._amulet_draw_ids[-remain:]
        remain_tiles = [id2tile.get(pid, "?") for pid in remain_ids]

        # 分行：第一行 r = remain % 9 张（若 r==0 则每行 9 张）
        def _chunk_by9_tail_first(seq: list[str]) -> list[list[str]]:
            n = len(seq)
            r = n % 9
            rows = []
            i = 0
            if r != 0:
                rows.append(seq[:r])
                i = r
            while i < n:
                rows.append(seq[i:i + 9])
                i += 9
            return rows

        # 转 emoji（失败退化为 "9m" 文本）
        def _as_emoji(ms_tile: str) -> str:
            try:
                from common.mj_helper import cvt_ms2mjai, MJAI_TILE_2_UNICODE
                return MJAI_TILE_2_UNICODE[cvt_ms2mjai(ms_tile)]
            except Exception:
                return ms_tile

        rows = _chunk_by9_tail_first(remain_tiles)
        line_strs = [" ".join(_as_emoji(t) for t in row) for row in rows]

        # 统计（仅统计剩余这 N 张；用于对照）
        from collections import Counter
        cnt = Counter(remain_tiles)
        stat_line = " ".join(f"{_as_emoji(t)}×{n}" for t, n in sorted(cnt.items(), key=lambda kv: kv[0]))

        header = f"[可摸剩余 {remain}/36]"
        return "\n".join([header] + line_strs + [stat_line])

    def get_amulet_replace_text(self) -> str:
        """
        渲染青云之志【换牌阶段】的待替换队列：
          - 取尾部49张作为固定队列（服务器顺序），转成 emoji；
          - 在队列中第 `cursor` 个位置插入光标“｜”（cursor==0 在最前，==len 在末尾）；
          - 每 9 个自动换行；
          - 光标不会超过剩余数量（min(cursor, len(queue))）。
        非 stage2 或数据缺失时返回空串。
        """
        # 必须处于 amulet 且有队列
        if not (self._amulet_active and self._amulet_pool and self._amulet_replace_ids):
            return ""

        # 读取 stage；仅在换牌阶段(stage==2)显示
        try:
            stage = int(self._amulet_info.get("stage", 0))
        except Exception:
            stage = 0
        if stage != 2:
            return ""

        # id -> tile
        id2tile: dict[int, str] = {}
        for obj in self._amulet_pool:
            try:
                id2tile[int(obj["id"])] = str(obj["tile"])
            except Exception:
                continue

        # 队列（固定49张顺序）
        tiles: list[str] = [id2tile.get(i, "?") for i in self._amulet_replace_ids]

        # 转 emoji
        def _as_emoji(ms_tile: str) -> str:
            try:
                from common.mj_helper import cvt_ms2mjai, MJAI_TILE_2_UNICODE
                return MJAI_TILE_2_UNICODE[cvt_ms2mjai(ms_tile)]
            except Exception:
                return ms_tile

        emojis: list[str] = [_as_emoji(t) for t in tiles]

        # 光标位置：已消耗数量；不超过当前队列长度
        cur = int(self._amulet_replace_cursor or 0)
        if cur < 0:
            cur = 0
        if cur > len(emojis):
            cur = len(emojis)

        # 在序列里插入光标"｜"（Unicode 全角竖线）
        seq: list[str] = emojis[:]
        seq.insert(cur, "｜")

        # 自动换行交由 Label 自己处理
        text_line = " ".join(seq)
        header = f"[待替换 {cur}/{len(emojis)}]"
        return f"{header}\n{text_line}"

    def _process_msg(self, msg:mitm.WSMessage):
        """ process websocket message from mitm server"""

        if msg.type == mitm.WsType.START:
            LOGGER.debug("Websocket Flow started: %s", msg.flow_id)

        elif msg.type == mitm.WsType.END:
            LOGGER.debug("Websocket Flow ended: %s", msg.flow_id)
            if msg.flow_id == self.game_flow_id:
                LOGGER.info("Game flow ended. processing end game")
                self._process_end_game()
                self.game_flow_id = None
            if msg.flow_id == self.lobby_flow_id:
                # lobby flow ended
                LOGGER.info("Lobby flow ended.")
                self.lobby_flow_id = None
                self.automation.on_exit_lobby()

        elif msg.type == mitm.WsType.MESSAGE:
            # process ws message
            try:
                liqimsg = self.liqi_parser.parse(msg.content)
            except Exception as e:
                LOGGER.warning("Failed to parse liqi msg: %s\nError: %s", msg.content, e)
                return
            liqi_id = liqimsg.get("id")
            liqi_type = liqimsg.get('type')
            liqi_method = liqimsg.get('method')
            liqi_data = liqimsg['data']
            # liqi_datalen = len(liqimsg['data'])

            if liqi_method in METHODS_TO_IGNORE:
                ...

            elif (liqi_type, liqi_method) == (liqi.MsgType.RES, liqi.LiqiMethod.oauth2Login):
                # lobby login msg
                if self.lobby_flow_id is None:  # record first time in lobby
                    LOGGER.info("Lobby oauth2Login msg: %s", liqimsg)
                    LOGGER.info("Lobby login done. lobby flow ID = %s", msg.flow_id)
                    self.lobby_flow_id = msg.flow_id
                    self.automation.on_lobby_login(liqimsg)
                else:
                    LOGGER.warning("Lobby flow exists %s, ignoring new lobby flow %s", self.lobby_flow_id, msg.flow_id)

            elif 'amulet' in liqi_method.lower():
                if liqi_type != liqi.MsgType.RES:
                    return
                LOGGER.debug('Sky-High Ambition msg: %s', dump_liqi_msg_str(liqimsg))
                if liqi_method == liqi.LiqiMethod.fetchAmuletActivityData:
                    self._amulet_active = True
                    self._amulet_on_fetch_data(liqi_data)
                elif liqi_method == liqi.LiqiMethod.amuletActivityUpgrade:
                    self._amulet_active = True
                    self._amulet_on_upgrade_events(liqi_data.get("events") or [])
                elif liqi_method == liqi.LiqiMethod.amuletActivityOperate:
                    self._amulet_active = True
                    self._amulet_on_operate_events(liqi_data.get("events") or [])
                elif liqi_method == liqi.LiqiMethod.amuletActivityGiveup:
                    self._amulet_active = False
                    self._amulet_pool = None
                    self._amulet_draw_ids = []
                    self._amulet_used_ids = set()
                    self._amulet_desktop_remain = 0
                    self._amulet_replace_ids = []
                    self._amulet_replace_cursor = 0
                    # 重置 GUI 简要信息
                    self._amulet_info.update({
                        'stage': 0,
                        'hands': [],
                        'desktop_remain': 0,
                        'ended': False,
                    })
                return


            elif msg.flow_id == self.lobby_flow_id:
                LOGGER.debug(
                    'Lobby msg(suppressed): id=%s, type=%s, method=%s, len=%d',
                    liqi_id, liqi_type, liqi_method, len(str(liqimsg)))

            else:
                LOGGER.debug('Other msg (ignored): %s', liqimsg)

    def _amulet_update_from_events(self, events: list[dict]) -> None:
        """
        从 amuletActivityOperate 的 events[] 更新青云之志的可观察状态。
        仅抽取 GUI 需要的关键信息：stage / hands / desktop_remain / ended
        """
        for ev in events:
            vc = ev.get("valueChanges", {}) or {}

            # stage
            if "stage" in vc:
                self._amulet_info["stage"] = vc["stage"]

            # round 内细项
            rd = vc.get("round") or {}
            # hands
            hands = rd.get("hands")
            if isinstance(hands, dict) and hands.get("dirty"):
                self._amulet_info["hands"] = hands.get("value") or []
            # desktopRemain
            dr = rd.get("desktopRemain")
            if isinstance(dr, dict) and dr.get("dirty"):
                self._amulet_info["desktop_remain"] = dr.get("value", 0)

            # ended
            if "ended" in vc:
                self._amulet_info["ended"] = bool(vc["ended"])

    def _process_idle_automation(self, liqimsg:dict):
        """ do some idle action based on liqi msg"""
        liqi_method = liqimsg['method']
        if liqi_method == liqi.LiqiMethod.NotifyGameBroadcast:  # reply to emoji
        # {'id': -1, 'type': <MsgType.Notify: 1>, 'method': '.lq.NotifyGameBroadcast',
        # 'data': {'seat': 2, 'content': '{"emo":7}'}}
            if liqimsg["data"]["seat"] != self.game_state.seat: # not self
                self.automation.automate_send_emoji()
        else:           # move mouse around randomly
            self.automation.automate_idle_mouse_move(0.05)

    def _process_end_game(self):
        # End game processes
        # self.game_flow_id = None
        self.game_state = None
        if self.browser:    # fix for corner case
            self.browser.overlay_clear_guidance()
        self.game_exception = None
        self.automation.on_end_game()


    def _update_overlay_conditions_met(self) -> bool:
        if not self.st.enable_overlay:
            return False
        if self.browser is None:
            return False
        if self.browser.is_page_normal() is False:
            return False
        return True


    def _update_overlay_guide(self):
        # Update overlay guide given pending reaction
        reaction = self.get_pending_reaction()
        if reaction:
            guide, options = mjai_reaction_2_guide(reaction, 3, self.st.lan())
            self.browser.overlay_update_guidance(guide, self.st.lan().OPTIONS_TITLE, options)
        else:
            self.browser.overlay_clear_guidance()


    def _update_overlay_botleft(self):
        # update overlay bottom left text
        # maj copilot
        text = '😸' + self.st.lan().APP_TITLE

        # Model
        model_text = '🤖'
        if self.is_bot_created():
            model_text += self.st.lan().MODEL + ": " + self.st.model_type
        else:
            model_text += self.st.lan().MODEL_NOT_LOADED

        # autoplay
        if self.st.enable_automation:
            autoplay_text = '✅' + self.st.lan().AUTOPLAY + ': ' + self.st.lan().ON
        else:
            autoplay_text = '⬛' + self.st.lan().AUTOPLAY + ': ' + self.st.lan().OFF
        if self.automation.is_running_execution():
            autoplay_text += "🖱️⏳"

        # line 4
        if self.main_thread_exception:
            line = '❌' + self.st.lan().MAIN_THREAD_ERROR
        elif self.game_exception:
            line = '❌' + self.st.lan().GAME_ERROR
        elif self.is_browser_zoom_off():
            line = '❌' + self.st.lan().CHECK_ZOOM
        elif self.is_game_syncing():
            line = '⏳'+ self.st.lan().SYNCING
        elif self.is_bot_calculating():
            line = '⏳'+ self.st.lan().CALCULATING
        elif self.is_in_game():
            line = '▶️' + self.st.lan().GAME_RUNNING
        else:
            line = '🟢' + self.st.lan().READY_FOR_GAME

        text = '\n'.join((text, model_text, autoplay_text, line))
        self.browser.overlay_update_botleft(text)

    def is_in_amulet(self) -> bool:
        return bool(self._amulet_active)

    def get_amulet_info(self) -> dict | None:
        # 返回浅拷贝，避免外部改内部
        return dict(self._amulet_info) if self._amulet_active else None

    def get_amulet_pending_action(self) -> dict | None:
        return self._amulet_pending_action

    def get_amulet_replace_queue(self) -> list[str]:
        """返回换牌队列（尾部49张）对应的 tile 字符串列表，保持服务器给定顺序。
        若当前无 Amulet 数据则返回空列表。"""
        if not (self._amulet_active and self._amulet_pool and self._amulet_replace_ids):
            return []
        # 建立 id->tile 映射
        id2tile: dict[int, str] = {}
        for obj in self._amulet_pool:
            try:
                id2tile[int(obj["id"])] = str(obj["tile"])
            except Exception:
                continue
        return [id2tile.get(pid, "?") for pid in self._amulet_replace_ids]

    def get_amulet_replace_cursor(self) -> int:
        """返回换牌队列游标（已消耗数量）。"""
        return int(self._amulet_replace_cursor or 0)

    
    def _do_automation(self, reaction:dict):
        # auto play given mjai reaction        
        if not reaction:    # no reaction given
            return False
        
        try:
            self.automation.automate_action(reaction, self.game_state)
        except Exception as e:
            LOGGER.error("Failed to automate action for %s: %s", reaction['type'], e, exc_info=True)

def dump_liqi_msg_str(liqimsg) -> str:
    """把 liqimsg 转成单行 JSON 字符串；布尔为 true/false。"""

    def to_jsonable(o):
        # 基础类型直接返回（包括 bool -> 会被正确序列化为 true/false）
        if o is None or isinstance(o, (str, int, float, bool)):
            return o

        # dict
        if isinstance(o, dict):
            return {str(k): to_jsonable(v) for k, v in o.items()}

        # list/tuple/set
        if isinstance(o, (list, tuple, set)):
            return [to_jsonable(x) for x in o]

        # protobuf Message（如可用就走官方转换，字段名保持原样）
        try:
            from google.protobuf.message import Message
            from google.protobuf.json_format import MessageToDict
            if isinstance(o, Message):
                return MessageToDict(o, preserving_proto_field_name=True)
        except Exception:
            pass

        # numpy 标量类型（若环境里有 numpy）
        try:
            import numpy as np
            if isinstance(o, np.bool_):
                return bool(o)
            if isinstance(o, np.integer):
                return int(o)
            if isinstance(o, np.floating):
                return float(o)
        except Exception:
            pass

        # Enum
        try:
            from enum import Enum
            if isinstance(o, Enum):
                return o.name
        except Exception:
            pass

        # 兜底：转字符串（只在万不得已时）
        return str(o)

    try:
        payload = to_jsonable(liqimsg)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"<Failed to dump liqimsg: {e}>"


def mjai_reaction_2_guide(reaction:dict, max_options:int=3, lan_str:LanStr=LanStr()) -> tuple[str, list]:
    """ Convert mjai reaction message to language specific AI guide 
    params:
        reaction(dict): reaction (output) message from mjai bot
        max_options(int): number of options to display. 0 to display no options
        lan_str(LanString): language specific string constants
        
    return:
        (action_str, options): action_str is the recommended action
        options is a list of options (str, float), each option being a tuple of tile str and a percentage number 
        
        sample output for Chinese:
        ("立直,切[西]", [("[西]", 0.9111111), ("立直", 0.077777), ("[一索]", 0.0055555)])        
        """
                
    if reaction is None:
        raise ValueError("Input reaction is None")
    re_type = reaction['type']
    
    def get_tile_str(mjai_tile:str):    # unicode + language specific name
        return MJAI_TILE_2_UNICODE[mjai_tile] + lan_str.mjai2str(mjai_tile)
    pai = reaction.get('pai', None)
    if pai:
        tile_str =  get_tile_str(pai)
    
    if re_type == MjaiType.DAHAI:
        action_str = f"{lan_str.DISCARD}{tile_str}"
    elif re_type == MjaiType.NONE:
        action_str = ActionUnicode.PASS + lan_str.PASS
    elif re_type == MjaiType.PON:
        action_str = f"{ActionUnicode.PON}{lan_str.PON}{tile_str}"
    elif re_type == MjaiType.CHI:
        comsumed = reaction['consumed']
        comsumed_strs = [f"{get_tile_str(x)}" for x in comsumed]
        action_str = f"{ActionUnicode.CHI}{lan_str.CHI}{tile_str}({''.join(comsumed_strs)})"         
    elif re_type == MjaiType.KAKAN:
        action_str = f"{ActionUnicode.KAN}{lan_str.KAN}{tile_str}({lan_str.KAKAN})"
    elif re_type == MjaiType.DAIMINKAN:
        action_str = f"{ActionUnicode.KAN}{lan_str.KAN}{tile_str}({lan_str.DAIMINKAN})"
    elif re_type == MjaiType.ANKAN:
        tile_str = get_tile_str(reaction['consumed'][1])
        action_str = f"{ActionUnicode.KAN}{lan_str.KAN}{tile_str}({lan_str.ANKAN})"
    elif re_type == MjaiType.REACH: # attach reach dahai options
        reach_dahai_reaction = reaction['reach_dahai']
        dahai_action_str, _dahai_options = mjai_reaction_2_guide(reach_dahai_reaction, 0, lan_str)
        action_str = f"{ActionUnicode.REACH}{lan_str.RIICHI}," + dahai_action_str
    elif re_type == MjaiType.HORA:
        if reaction['actor'] == reaction['target']:
            action_str = f"{ActionUnicode.AGARI}{lan_str.AGARI}({lan_str.TSUMO})"
        else:
            action_str = f"{ActionUnicode.AGARI}{lan_str.AGARI}({lan_str.RON})"
    elif re_type == MjaiType.RYUKYOKU:
        action_str = f"{ActionUnicode.RYUKYOKU}{lan_str.RYUKYOKU}"
    elif re_type == MjaiType.NUKIDORA:
        action_str = f"{lan_str.NUKIDORA}{MJAI_TILE_2_UNICODE['N']}"
    else:
        action_str = lan_str.mjai2str(re_type)
    
    options = []
    if max_options > 0 and 'meta_options' in reaction:
        # process options. display top options with their weights
        meta_options = reaction['meta_options'][:max_options]
        if meta_options:
            for (code, q) in meta_options:      # code is in MJAI_MASK_LIST                
                if code in MJAI_TILES_34 or code in MJAI_AKA_DORAS:
                    # if it is a tile
                    name_str = get_tile_str(code)
                elif code == MjaiType.NUKIDORA:
                    name_str = lan_str.mjai2str(code) + MJAI_TILE_2_UNICODE['N']
                else:
                    name_str = lan_str.mjai2str(code)                
                options.append((name_str, q))
        
    return (action_str, options)
