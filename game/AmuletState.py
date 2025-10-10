from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

import liqi
from common.log_helper import LOGGER


@dataclass
class AmuletState:
    stage: int = 0
    hands: List[int] = field(default_factory=list)   # 你抓到的 round.hands.value
    desktop_remain: int = 0
    ended: bool = False
    # 需要就加：effects/shop/record 等

class AmuletGameState:
    def __init__(self, bot) -> None:
        self.state = AmuletState()
        self.bot = bot  # 建议做一个 AmuletBot，返回“选择第几个/是否重开/是否购物”等动作
        self._pending: Optional[Dict[str, Any]] = None

    def get_pending_reaction(self) -> Optional[Dict[str, Any]]:
        return self._pending

    def input(self, liqi_msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        m = liqi_msg.get("method","")
        t = liqi_msg.get("type")
        if "amulet" not in m.lower():
            return None
        if t != liqi.MsgType.RES:
            return None

        data = liqi_msg.get("data",{})
        # 解析 events
        for ev in data.get("events", []):
            self._apply_event(ev)

        # 交给策略决定下一步（返回你定义的动作结构；下面示例）
        action = self.bot.decide(self.state)
        self._pending = action
        LOGGER.info("AmuletBot out: %s", action)
        return action

    def _apply_event(self, ev: Dict[str, Any]) -> None:
        vc = ev.get("valueChanges", {})
        if "stage" in vc:
            self.state.stage = vc["stage"]
        round_v = vc.get("round", {})
        if "hands" in round_v:
            if round_v["hands"].get("dirty"):
                self.state.hands = round_v["hands"].get("value", [])
        if "desktopRemain" in round_v:
            if round_v["desktopRemain"].get("dirty"):
                self.state.desktop_remain = round_v["desktopRemain"]["value"]
        if vc.get("ended") is True:
            self.state.ended = True