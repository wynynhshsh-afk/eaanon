from __future__ import annotations

from typing import Dict, List

from adapters.base import Button, Keyboard
from . import texts as T


def main_menu() -> Keyboard:
    return [
        [Button(T.BTN_RANDOM, "menu:random")],
        [Button(T.BTN_LINK, "menu:link")],
        [Button(T.BTN_INBOX, "menu:inbox")],
        [Button(T.BTN_HELP, "menu:help")],
    ]


def gender_self() -> Keyboard:
    return [[Button(T.BTN_BOY, "g:male"), Button(T.BTN_GIRL, "g:female")],
            [Button(T.BTN_CANCEL, "q:cancel")]]


def gender_want() -> Keyboard:
    return [[Button(T.BTN_WANT_GIRL, "w:female"), Button(T.BTN_WANT_BOY, "w:male")],
            [Button(T.BTN_WANT_ANY, "w:any")],
            [Button(T.BTN_CANCEL, "q:cancel")]]


def searching() -> Keyboard:
    return [[Button(T.BTN_CANCEL, "q:cancel")]]


def chat_controls() -> Keyboard:
    return [[Button(T.BTN_END, "chat:end")]]


def cancel_only() -> Keyboard:
    return [[Button(T.BTN_CANCEL, "q:cancel")]]


def to_menu() -> Keyboard:
    return [[Button(T.BTN_MENU, "menu:home")]]


def link_view() -> Keyboard:
    return [[Button(T.BTN_NEW_LINK, "link:new")], [Button(T.BTN_MENU, "menu:home")]]


def view_message(msg_id: int) -> Keyboard:
    return [[Button(T.BTN_VIEW, f"view:{msg_id}")]]


def message_actions(msg_id: int, offset: int, total: int, in_inbox: bool) -> Keyboard:
    rows: List[List[Button]] = []
    if in_inbox:
        nav = []
        if offset > 0:
            nav.append(Button(T.BTN_PREV, f"inbox:{offset - 1}"))
        if offset + 1 < total:
            nav.append(Button(T.BTN_NEXT, f"inbox:{offset + 1}"))
        if nav:
            rows.append(nav)
    rows.append([Button(T.BTN_REPLY, f"reply:{msg_id}"), Button(T.BTN_BLOCK, f"block:{msg_id}")])
    rows.append([Button(T.BTN_REPORT, f"report:{msg_id}"),
                 Button(T.BTN_DELETE, f"del:{msg_id}:{offset}")])
    rows.append([Button(T.BTN_MENU, "menu:home")])
    return rows


# متن دکمه → داده (برای حالت جایگزین متنی وقتی دکمه اینلاین پشتیبانی نشود)
LABEL_TO_DATA: Dict[str, str] = {}
for _kb in (main_menu(), gender_self(), gender_want(), chat_controls(), to_menu()):
    for _row in _kb:
        for _b in _row:
            LABEL_TO_DATA.setdefault(_b.text, _b.data)
