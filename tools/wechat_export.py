# -*- coding: utf-8 -*-
"""wechat_export — 微信会话全量导出工具（纯后台、无 GUI、不动鼠标键盘）

能力：
- 全消息类型解析：文本/引用/聊天记录(合并转发)/系统消息/图片/视频/语音/文件/链接/表情/红包/位置
- 发送者实名解析：Name2Id + 通讯录昵称/备注 + 群名片(chat_room.ext_buffer)
- 媒体落盘：图片 AES 解密（密钥持久化在 image_keys.json）、视频/文件复制（全树按名兜底）、
  语音 SILK 提取并转 24kHz WAV（pysilk，失败保留 .silk）
- 路径定位：每条媒体消息写入 media_path（导出文件绝对路径）/source_path（微信侧原始路径），
  生成 media_index.json，支持 --locate 直查
- 语音转录：导出时免费提取微信原生"转文字"结果（transcript_source=wechat_native）；
  --asr/--transcribe 用豆包 ASR 补转（密钥与工作台共用 data/workbench_settings.json）
- 快照式读取：sqlite backup 到临时副本，避免与微信/其他进程并发写冲突
- 校验报告：类型分布、媒体覆盖率、缺失清单
- watch 模式：后台轮询增量抓取新消息

用法（在 WeChat_CatchCatch 目录下，用 .venv 的 python）：
    .venv/Scripts/python tools/wechat_export.py --chat 26级新生团组织关系转接帮帮群
    .venv/Scripts/python tools/wechat_export.py --chat 26级新生 --watch 120
    .venv/Scripts/python tools/wechat_export.py --list
    .venv/Scripts/python tools/wechat_export.py --chat 26级新生 --locate
    .venv/Scripts/python tools/wechat_export.py --chat 26级新生 --locate --lid 188
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import zstandard as zstd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from wechat_bridge.voice import DoubaoASRClient, extract_wechat_voice_transcript  # noqa: E402
from wechatauto import WeChatDB  # noqa: E402
from wechatauto.media import MediaDownloader, V2_MAGIC  # noqa: E402

CST = timezone(timedelta(hours=8))
DCTX = zstd.ZstdDecompressor()
BASE_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "wechat_export")

MSG_TYPE = {
    1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
    48: "location", 49: "app", 50: "voip", 42: "card", 10000: "system",
}
APP_SUBTYPE = {
    4: "videoshare", 5: "link", 6: "file", 7: "miniprogram", 8: "card",
    19: "history", 24: "note", 33: "miniprogram", 36: "miniprogram",
    51: "card", 57: "quote", 62: "pat", 63: "card", 87: "note",
    124: "gift", 2000: "transfer",
}
RED_PACKET_LT = 0x7D100000031


# ---------------------------------------------------------------- 基础工具

def zdec(b):
    if not b:
        return b""
    if isinstance(b, str):
        return b.encode("utf-8", "replace")
    if b[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return DCTX.decompress(b, max_output_size=20 * 1024 * 1024)
        except Exception:
            return b""
    return b


def xtext(elem, path):
    n = elem.find(path)
    return (n.text or "").strip() if n is not None and n.text else ""


def parse_xml(raw: bytes):
    if b"\n" in raw[:80]:
        head, body = raw.split(b"\n", 1)
        if head.decode("utf-8", "ignore").endswith("@chatroom"):
            raw = body
    for candidate in (raw, raw[re.search(rb"<[a-zA-Z?]", raw).start():] if re.search(rb"<[a-zA-Z?]", raw) else b""):
        if not candidate:
            continue
        try:
            return ET.fromstring(candidate.decode("utf-8", "replace"))
        except ET.ParseError:
            continue
    return None


def strip_xml_to_text(t: str) -> str:
    t = html.unescape(t or "")
    if not t.lstrip().startswith("<"):
        return t.strip()
    m = re.search(r"<title>(.*?)</title>", t, re.S)
    title = m.group(1).strip() if m else ""
    if "announcement" in t or "group_notice" in t:
        return ("[群公告] " + title).strip()
    if title:
        return title
    plain = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", plain).strip()[:200]


def render_sysmsg(root) -> str:
    tmpl = root.find(".//content_template/template")
    if tmpl is None or not tmpl.text:
        plain = root.find(".//content_template/plain")
        return (plain.text or "").strip() if plain is not None else "[系统消息]"
    out = tmpl.text
    for link in root.findall(".//link_list/link"):
        name = link.get("name") or ""
        members = []
        for m in link.findall("memberlist/member"):
            nick = m.find("nickname")
            members.append((nick.text or "").strip() if nick is not None else "")
        if not members:
            nick = link.find("nickname")
            if nick is not None and nick.text:
                members.append(nick.text.strip())
        out = out.replace("$%s$" % name, "、".join('"%s"' % x for x in members if x) or "?")
    return re.sub(r"\$[a-zA-Z_]+\$", "?", out).strip()


def parse_room_ext(buf: bytes):
    """chat_room.ext_buffer → wxid -> {alias, inviter}（松散 protobuf 解析）"""
    members = {}
    if not buf:
        return members
    for m in re.finditer(rb"\x0a\x13(wxid_[0-9a-z_]+)", buf):
        wxid = m.group(1).decode()
        tail = buf[m.end(): m.end() + 90]
        alias = ""
        am = re.match(rb"\x12([\x08-\x7f])", tail)
        if am:
            ln = am.group(1)[0]
            alias = tail[2: 2 + ln].decode("utf-8", "replace")
        inv = ""
        im = re.search(rb"\x22\x13(wxid_[0-9a-z_]+)", tail)
        if im:
            inv = im.group(1).decode()
        members[wxid] = {"alias": alias, "inviter": inv}
    return members


# ---------------------------------------------------------------- 快照读取

class SnapshotDB:
    """把解密后的库用 sqlite backup 复制成稳定快照再读，杜绝并发写导致的 malformed。"""

    def __init__(self, db: WeChatDB):
        self.db = db
        self.tmpdir = tempfile.mkdtemp(prefix="wxsnap_")
        self._cache: dict[str, str] = {}

    def snapshot(self, rel: str) -> str:
        dst = os.path.join(self.tmpdir, rel.replace(os.sep, "__"))
        last_err = None
        for _ in range(5):
            try:
                src = self.db._open(rel)
                try:
                    tgt = sqlite3.connect(dst)
                    src.backup(tgt)
                    tgt.close()
                finally:
                    src.close()
                self._cache[rel] = dst
                return dst
            except sqlite3.DatabaseError as e:
                last_err = e
                time.sleep(2 + _ * 2)
        raise RuntimeError(f"快照失败 {rel}: {last_err}")

    def connect(self, rel: str) -> sqlite3.Connection:
        path = self._cache.get(rel) or self.snapshot(rel)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.text_factory = bytes
        return con


# ---------------------------------------------------------------- 主导出器

class ChatExporter:
    def __init__(self, out_root=BASE_OUT):
        self.db = WeChatDB()
        self.snap = SnapshotDB(self.db)
        self.md = MediaDownloader(self.db)
        self.out_root = out_root
        self.img_key = self._load_img_key()

    # ---------- 密钥 ----------
    def _load_img_key(self):
        ks = os.path.join(self.db.workdir, "image_keys.json")
        try:
            saved = json.load(open(ks, encoding="utf-8"))
            key = saved.get(self.db.account)
            if key:
                return key
        except (OSError, ValueError):
            pass
        return None

    def _validate_img_key(self, probe_dat: str) -> bool:
        if not self.img_key:
            return False
        try:
            with open(probe_dat, "rb") as f:
                head = f.read(64)
            if head[:6] != V2_MAGIC:
                return True  # 非 V2 无需 AES
            probe = head[15:31]
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            dec = Cipher(algorithms.AES(self.img_key.encode()), modes.ECB()).decryptor()
            pt = dec.update(probe) + dec.finalize()
            return pt[:3] == b"\xff\xd8\xff" or pt[:4] in (b"\x89PNG", b"GIF8", b"RIFF", b"wxgf")
        except Exception:
            return False

    # ---------- 会话定位 ----------
    def find_chats(self, keyword: str):
        con = self.snap.connect("contact\\contact.db")
        rows = con.execute(
            "select username, nick_name, remark from contact where username like '%@chatroom'"
        ).fetchall()
        hits = []
        for u, n, r in rows:
            u = u.decode() if isinstance(u, bytes) else u
            n = (n or b"").decode("utf-8", "replace") if isinstance(n, bytes) else (n or "")
            r = (r or b"").decode("utf-8", "replace") if isinstance(r, bytes) else (r or "")
            if keyword in n or keyword in r or keyword in u:
                hits.append({"username": u, "name": n, "remark": r})
        return hits

    def _msg_tables(self, username: str):
        md5 = hashlib.md5(username.encode()).hexdigest()
        found = []
        for rel, path, _ in self.db._db_files:
            base = os.path.basename(path)
            if not base.startswith("message_") or not base.endswith(".db"):
                continue
            try:
                con = self.snap.connect(rel)
                t = f"Msg_{md5}"
                n = con.execute(
                    "select count(*) from sqlite_master where type='table' and name=?", (t,)
                ).fetchone()[0]
                if n:
                    found.append((rel, t))
            except (sqlite3.DatabaseError, RuntimeError):
                continue
        return found

    # ---------- 身份解析 ----------
    def _load_identities(self, username: str, shard_rel: str):
        id2name = {}
        con = self.snap.connect(shard_rel)
        for rid, u in con.execute("select rowid, user_name from Name2Id"):
            id2name[rid] = u.decode() if isinstance(u, bytes) else u
        contact = {}
        ccon = self.snap.connect("contact\\contact.db")
        for u, n, r in ccon.execute("select username, nick_name, remark from contact"):
            u = u.decode() if isinstance(u, bytes) else u
            n = (n or b"").decode("utf-8", "replace") if isinstance(n, bytes) else (n or "")
            r = (r or b"").decode("utf-8", "replace") if isinstance(r, bytes) else (r or "")
            contact[u] = {"nick": n, "remark": r}
        room_members, owner, announcement = {}, "", ""
        row = ccon.execute("select owner, ext_buffer from chat_room where username=?", (username,)).fetchone()
        if row:
            owner = row[0].decode() if isinstance(row[0], bytes) else (row[0] or "")
            room_members = parse_room_ext(row[1])
        arow = ccon.execute(
            "select announcement_, announcement_editor_, announcement_publish_time_ "
            "from chat_room_info_detail where username_=?", (username,)).fetchone()
        if arow and arow[0]:
            a = arow[0].decode("utf-8", "replace") if isinstance(arow[0], bytes) else arow[0]
            ed = arow[1].decode() if isinstance(arow[1], bytes) else (arow[1] or "")
            pt_ = arow[2] or 0
            announcement = f"{a}\n（编辑者 {ed}，时间 {datetime.fromtimestamp(pt_, CST):%Y-%m-%d %H:%M}）"

        def display(wxid):
            if not wxid:
                return "?"
            alias = (room_members.get(wxid) or {}).get("alias") or ""
            c = contact.get(wxid) or {}
            return alias or c.get("remark") or c.get("nick") or wxid

        return id2name, contact, room_members, owner, announcement, display

    # ---------- 单条消息解析 ----------
    def _parse_message(self, lid, ltype, sid, ctime, raw, id2name, display, is_group, packed=b""):
        base = ltype & 0xFFFFFFFF
        sub = ltype >> 32
        sender_wxid = id2name.get(sid, "")
        body = raw
        m = re.match(rb"([A-Za-z][A-Za-z0-9_-]{4,24}|[0-9]+@chatroom):\n", raw)
        if m:
            pref = m.group(1).decode()
            if pref.endswith("@chatroom"):
                body = raw[m.end():]
            else:
                sender_wxid = pref
                body = raw[m.end():]
        ts = datetime.fromtimestamp(ctime, CST).strftime("%Y-%m-%d %H:%M:%S")
        text = body.decode("utf-8", "replace").strip("\x00").strip()
        kind = MSG_TYPE.get(base, f"type_{base}")
        if ltype == RED_PACKET_LT:
            kind = "redpacket"
        item = {
            "local_id": lid, "time": ts, "kind": kind,
            "sender_wxid": sender_wxid,
            "sender": "系统" if base == 10000 else (display(sender_wxid) if sender_wxid else ("系统" if sid == 19 else "?")),
        }
        root = None
        if base in (3, 43, 47, 48, 49, 42, 10000) or ltype == RED_PACKET_LT:
            root = parse_xml(body)

        if kind == "text":
            item["text"] = text
        elif kind == "pat":
            item["text"] = item.get("title") or text
        elif kind == "system":
            item["text"] = render_sysmsg(root) if root is not None else (text or "[系统消息]")
        elif kind == "image":
            md5 = ""
            if root is not None and root.find("img") is not None:
                md5 = root.find("img").get("md5") or ""
            if not md5:
                mm = re.search(rb'md5="([0-9a-fA-F]{32})"', body)
                md5 = mm.group(1).decode() if mm else ""
            item["md5"] = md5
        elif kind == "video":
            mm = re.search(rb'md5="([0-9a-fA-F]{32})"', body)
            item["md5"] = mm.group(1).decode() if mm else ""
        elif kind == "voice":
            item["text"] = "[语音]"
            native = extract_wechat_voice_transcript(packed)
            if native:
                item["transcript"] = native
                item["transcript_source"] = "wechat_native"
        elif kind == "emoji":
            e = root.find("emoji") if root is not None else None
            item["cdnurl"] = (e.get("cdnurl") or "") if e is not None else ""
        elif kind == "location":
            item["text"] = text or "[位置]"
            if root is not None:
                loc = root.find("location")
                if loc is not None:
                    item["text"] = f"[位置] {loc.get('label') or loc.get('poiname') or ''} {loc.get('x')},{loc.get('y')}"
        elif kind == "redpacket":
            item["text"] = "[红包]"
            if root is not None:
                wcp = root.find(".//wcpayinfo")
                if wcp is not None:
                    pay = {c.tag: (c.text or "").strip() for c in wcp if (c.text or "").strip()}
                    keep = {k: pay[k] for k in ("sendertitle", "senderdes", "scenetext", "invalidtime") if k in pay}
                    if keep:
                        item["wcpayinfo"] = keep
                    label = keep.get("sendertitle") or keep.get("senderdes") or ""
                    scene = keep.get("scenetext") or ""
                    item["text"] = "[红包]" + (f"({scene})" if scene else "") + (f" {label}" if label else "")
        elif kind == "card" and base == 42:
            # 裸名片消息（type 42）：昵称/微信号在根元素 <msg> 的属性上
            if root is not None:
                prof = root.find(".//profileitem")
                src = prof if prof is not None else root
                item["card_nick"] = src.get("nickname") or ""
                item["card_wxid"] = (src.get("username") or "")[:40]
                item["text"] = f"[名片] {item['card_nick']} ({item['card_wxid']})"
        elif kind == "app":
            sub_kind = APP_SUBTYPE.get(sub, f"app_{sub}")
            item["kind"] = sub_kind
            item["title"] = ""
            if root is not None:
                app = root.find("appmsg")
                if app is not None:
                    item["title"] = xtext(app, "title")
                    item["url"] = xtext(app, "url")
                    item["desc"] = xtext(app, "des")
                if sub_kind == "quote":
                    ref = root.find(".//refermsg")
                    if ref is not None:
                        item["quote_from"] = xtext(ref, "displayname")
                        item["quote_content"] = strip_xml_to_text(xtext(ref, "content"))[:300]
                elif sub_kind == "history":
                    rec = app.find("recorditem") if app is not None else None
                    lines = []
                    if rec is not None and rec.text:
                        try:
                            rroot = ET.fromstring(html.unescape(rec.text))
                            for di in rroot.findall(".//dataitem"):
                                sn = xtext(di, "sourcename")
                                dd = xtext(di, "datadesc")
                                st = xtext(di, "sourcetime")
                                dt = di.get("datatype") or ""
                                lines.append(f"{sn}({st}): {dd or ('[' + dt + ']' if dt else '')}")
                        except ET.ParseError:
                            pass
                    item["record_lines"] = lines
                elif sub_kind == "note":
                    item["title"] = "[群公告卡片]"
                elif sub_kind == "transfer":
                    item["title"] = item["title"] or "[转账]"
                elif sub_kind == "card":
                    nick = xtext(app, "nickname")
                    wxid = xtext(app, "username")
                    if nick:
                        item["card_nick"] = nick
                        item["card_wxid"] = wxid
                        item["title"] = f"名片: {nick}" + (f" ({wxid})" if wxid else "")
        else:
            item["text"] = text[:200]
        return item

    # ---------- 媒体 ----------
    def _resource_map(self, username: str):
        """message_local_id -> (stored_md5, detail_filename)"""
        res = {}
        rel = None
        for r, path, _ in self.db._db_files:
            if os.path.basename(path) == "message_resource.db":
                rel = r
                break
        if not rel:
            return res
        try:
            con = self.snap.connect(rel)
        except RuntimeError:
            return res
        cid_row = con.execute("select rowid from ChatName2Id where user_name=?", (username,)).fetchone()
        if not cid_row:
            return res
        cid = cid_row[0]
        for rowid, lid, pi in con.execute(
            "select rowid, message_local_id, packed_info from MessageResourceInfo where chat_id=?", (cid,)
        ):
            m = re.search(rb"([0-9a-fA-F]{32})", pi or b"")
            md5 = m.group(1).decode() if m else ""
            fn = ""
            d = con.execute(
                "select packed_info from MessageResourceDetail where message_id=? limit 1", (rowid,)
            ).fetchone()
            if d and d[0]:
                fm = re.search(rb"\x0a.\x0a..(.{4,120}?)\x12", d[0], re.S)
                if fm:
                    cand = fm.group(1).decode("utf-8", "replace")
                    # 只接受可打印且含扩展名的文件名
                    if re.match(r"^[\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+\.[A-Za-z0-9]{2,5}$", cand):
                        fn = cand
            res[lid] = (md5, fn)
        return res

    # ---------- 媒体 ----------
    def _voice_index(self, username: str):
        """svr_id -> voice_data：扫描全部 media_*.db 的 VoiceInfo（带重试的直读，避免快照大库开销）。"""
        idx = {}
        for rel, path, _ in self.db._db_files:
            if not os.path.basename(path).startswith("media_"):
                continue
            last_err = None
            for attempt in range(3):
                conn = None
                try:
                    conn = self.db._open(rel)
                    cid = conn.execute(
                        "SELECT rowid FROM Name2Id WHERE user_name=?", (username,)
                    ).fetchone()
                    if not cid:
                        break
                    for svr, data in conn.execute(
                        "SELECT svr_id, voice_data FROM VoiceInfo WHERE chat_name_id=? AND voice_data IS NOT NULL",
                        (cid[0],),
                    ):
                        if svr and data:
                            idx[svr] = data
                    break
                except sqlite3.DatabaseError as e:
                    last_err = e
                    time.sleep(1 + attempt * 2)
                finally:
                    if conn is not None:
                        conn.close()
            else:
                print(f"  [voice] {os.path.basename(path)} 读取失败: {str(last_err)[:60]}", flush=True)
        return idx

    @staticmethod
    def _silk_to_wav(silk_path: str, wav_path: str, rate: int = 24000) -> bool:
        """SILK -> 可播放的 16bit 单声道 WAV（微信语音采样率 24kHz）。失败返回 False 保留 .silk。"""
        import io as _io
        import wave as _wave

        import pysilk
        try:
            with open(silk_path, "rb") as f:
                raw = f.read()
            if raw[:1] == b"\x02":
                raw = raw[1:]
            if not raw.startswith(b"#!SILK_V3"):
                raw = b"#!SILK_V3" + raw
            pcm = _io.BytesIO()
            pysilk.decode(_io.BytesIO(raw), pcm, rate)
            with _wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm.getvalue())
            return True
        except Exception:  # noqa: BLE001
            return False

    def _find_local_file(self, name: str, month: str, subdirs=("file",)) -> str:
        """按文件名在 msg/<subdirs> 下定位：先走消息当月目录，再全树兜底（解决下载月份与发消息月份错位）。"""
        if not name:
            return ""
        acc = self.db.account_dir
        for sub in subdirs:
            direct = os.path.join(acc, "msg", sub, month, name)
            if os.path.isfile(direct):
                return direct
            base = os.path.join(acc, "msg", sub)
            for root_, _, files_ in os.walk(base):
                if name in files_:
                    return os.path.join(root_, name)
        return ""

    def _month(self, ctime):
        return time.strftime("%Y-%m", time.localtime(ctime))

    def fetch_media(self, username, items, times, outdir, svrs=None):
        os.makedirs(outdir, exist_ok=True)
        res_map = self._resource_map(username)
        chat_md5 = hashlib.md5(username.encode()).hexdigest()
        acc = self.db.account_dir
        report = {"image": [0, 0], "video": [0, 0], "file": [0, 0], "voice": [0, 0], "emoji": [0, 0], "missing": []}
        svrs = svrs or {}
        vmap = self._voice_index(username) if any(
            it["kind"] == "voice" for it in items) else {}
        emoji_cache = {}
        key_checked = None

        def mark_saved(it, fname):
            it["media_file"] = fname
            it["media_path"] = os.path.abspath(os.path.join(outdir, fname))

        for it in items:
            lid, kind = it["local_id"], it["kind"]
            ctime = times.get(lid, int(time.time()))
            month = self._month(ctime)
            stored_md5, detail_fn = res_map.get(lid, ("", ""))
            try:
                if kind == "image":
                    report["image"][1] += 1
                    md5 = stored_md5 or it.get("md5") or ""
                    dat = None
                    base = os.path.join(acc, "msg", "attach", chat_md5)
                    for suffix in (".dat", "_h.dat", "_t.dat"):
                        cand = os.path.join(base, month, "Img", md5 + suffix)
                        if md5 and os.path.isfile(cand):
                            dat = cand
                            break
                    if not dat:
                        for root_, _, files_ in os.walk(base):
                            for f in files_:
                                if f.startswith(md5) and f.endswith(".dat"):
                                    dat = os.path.join(root_, f)
                                    break
                            if dat:
                                break
                    if not dat:
                        report["missing"].append({"lid": lid, "kind": "image", "reason": "no_dat"})
                        continue
                    it["source_path"] = dat
                    thumb = dat.endswith("_t.dat")
                    last_err = None
                    data = None
                    with open(dat, "rb") as f:
                        is_v2 = f.read(6) == V2_MAGIC
                    if is_v2:
                        if key_checked is None:
                            key_checked = self._validate_img_key(dat)
                        if not key_checked:
                            report["missing"].append({"lid": lid, "kind": "image", "reason": "bad_key"})
                            continue
                        for _ in range(4):
                            try:
                                data = self.md.decrypt_image(dat, self.img_key)
                                break
                            except Exception as e:
                                last_err = e
                                time.sleep(1.5)
                    else:
                        # v1/XOR 旧加密：单字节异或，按文件头推导密钥整文件解码
                        try:
                            k = self.md._derive_xor_key(dat)
                            with open(dat, "rb") as f:
                                data = bytes(b ^ k for b in f.read())
                        except Exception:
                            data = None
                        if data and data[:3] not in (b"\xff\xd8\xff", b"\x89PN", b"GIF", b"RIFF"):
                            data = None
                    if data is None:
                        report["missing"].append({"lid": lid, "kind": "image",
                                                  "reason": str(last_err or "xor_decode_failed")[:60]})
                        continue
                    ext = "jpg"
                    if data[:4] == b"\x89PNG":
                        ext = "png"
                    elif data[:3] == b"GIF":
                        ext = "gif"
                    elif data[:4] == b"wxgf":
                        jpg = self.md._wxgf_to_jpg(data)
                        if jpg is not None:
                            data = jpg
                        else:
                            ext = "wxgf"
                    fn = f"{lid}{'_thumb' if thumb else ''}.{ext}"
                    with open(os.path.join(outdir, "img_" + fn), "wb") as f:
                        f.write(data)
                    report["image"][0] += 1
                    mark_saved(it, "img_" + fn)
                elif kind == "video":
                    report["video"][1] += 1
                    md5 = stored_md5 or it.get("md5") or ""
                    src = self._find_local_file(md5 + ".mp4", month, subdirs=("video",))
                    if md5 and src:
                        dst = os.path.join(outdir, f"video_{lid}.mp4")
                        shutil.copy2(src, dst)
                        os.chmod(dst, 0o666)
                        report["video"][0] += 1
                        mark_saved(it, f"video_{lid}.mp4")
                    else:
                        th = os.path.join(acc, "msg", "video", month, md5 + "_thumb.jpg")
                        if md5 and os.path.isfile(th):
                            shutil.copy2(th, os.path.join(outdir, f"video_{lid}_thumb.jpg"))
                            it["media_file"] = f"video_{lid}_thumb.jpg"
                            it["media_path"] = os.path.abspath(os.path.join(outdir, it["media_file"]))
                        it["source_path"] = os.path.join(acc, "msg", "video", month, md5 + ".mp4")
                        report["missing"].append({"lid": lid, "kind": "video", "reason": "not_downloaded"})
                elif kind == "file":
                    report["file"][1] += 1
                    name = detail_fn or it.get("title") or ""
                    src = self._find_local_file(name, month, subdirs=("file",))
                    if src:
                        safe = re.sub(r'[\\/:*?"<>|]', "_", os.path.basename(src))
                        dst = os.path.join(outdir, f"file_{lid}_{safe}")
                        shutil.copy2(src, dst)
                        os.chmod(dst, 0o666)
                        report["file"][0] += 1
                        mark_saved(it, os.path.basename(dst))
                    else:
                        it["source_path"] = os.path.join(acc, "msg", "file", month, name)
                        report["missing"].append({"lid": lid, "kind": "file", "reason": "not_local", "name": name})
                elif kind == "voice":
                    report["voice"][1] += 1
                    svr = svrs.get(lid)
                    data = vmap.get(svr) if svr else None
                    if not data:
                        # 兜底：走包内原始通道（live 读，按 local_id 重查）
                        try:
                            p = self.md.download_voice(username, lid, save_dir=outdir)
                        except Exception:
                            p = None
                        if p and os.path.isfile(p):
                            data_path = p
                        else:
                            it["source_path"] = f"media_*.db VoiceInfo (svr_id={svr or '?'})"
                            report["missing"].append({"lid": lid, "kind": "voice",
                                                      "reason": "no_voicedata" if svr else "no_svr"})
                            continue
                    else:
                        data_path = os.path.join(outdir, f"voice_{lid}.silk")
                        with open(data_path, "wb") as f:
                            f.write(data)
                    wav = os.path.splitext(data_path)[0] + ".wav"
                    if self._silk_to_wav(data_path, wav):
                        os.remove(data_path)
                        mark_saved(it, os.path.basename(wav))
                    else:
                        mark_saved(it, os.path.basename(data_path))
                    report["voice"][0] += 1
                elif kind == "emoji":
                    report["emoji"][1] += 1
                    url = (it.get("cdnurl") or "").strip()
                    if not url:
                        report["missing"].append({"lid": lid, "kind": "emoji", "reason": "no_cdnurl"})
                        continue
                    key = hashlib.md5(url.encode()).hexdigest()[:10]
                    if key in emoji_cache:
                        mark_saved(it, emoji_cache[key])
                        report["emoji"][0] += 1
                        continue
                    data = None
                    for _ in range(2):
                        try:
                            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(req, timeout=12) as resp:
                                data = resp.read()
                            break
                        except Exception:
                            time.sleep(1)
                    if not data:
                        report["missing"].append({"lid": lid, "kind": "emoji", "reason": "cdn_fetch_failed"})
                        continue
                    if data[:3] == b"GIF":
                        ext = "gif"
                    elif data[:4] == b"\x89PNG":
                        ext = "png"
                    elif data[:2] == b"\xff\xd8":
                        ext = "jpg"
                    else:
                        jpg = self.md._wxgf_to_jpg(data)
                        if jpg is not None:
                            data, ext = jpg, "jpg"
                        else:
                            ext = "bin"
                    fname = f"emoji_{key}.{ext}"
                    with open(os.path.join(outdir, fname), "wb") as f:
                        f.write(data)
                    emoji_cache[key] = fname
                    report["emoji"][0] += 1
                    mark_saved(it, fname)
            except Exception as e:  # noqa: BLE001
                report["missing"].append({"lid": lid, "kind": kind, "reason": str(e)[:80]})
        return report

    # ---------- 渲染 ----------
    def render_line(self, it):
        k = it["kind"]
        s = it["sender"]
        t = it["time"]
        if k == "text":
            return f"[{t}] {s}: {it.get('text', '')}"
        if k == "system":
            return f"[{t}] <系统> {it.get('text', '')}"
        if k == "quote":
            return f"[{t}] {s}: {it.get('title', '')}  (引用 {it.get('quote_from', '')}: {it.get('quote_content', '')})"
        if k == "history":
            lines = [f"[{t}] {s}: [聊天记录] {it.get('title', '')}"]
            lines += [f"    ↳ {l}" for l in it.get("record_lines", [])]
            return "\n".join(lines)
        if k == "image":
            mf = it.get("media_file") or ""
            return f"[{t}] {s}: [图片{(' → ' + mf) if mf else ''}]"
        if k == "video":
            note = "已保存" if it.get("media_file", "").endswith(".mp4") else "未下载-占位提醒"
            return f"[{t}] {s}: [视频-{note} {it.get('media_file', '')}]"
        if k == "voice":
            tr = it.get("transcript", "")
            tr_part = f" ↳{tr[:80]}" if tr else ""
            return f"[{t}] {s}: [语音 {it.get('media_file', '')}{tr_part}]"
        if k == "file":
            return f"[{t}] {s}: [文件] {it.get('title', '')} {('→ ' + it['media_file']) if it.get('media_file') else ''}"
        if k == "emoji":
            mf = it.get("media_file", "")
            return f"[{t}] {s}: [表情包{(' → ' + mf) if mf else ''}]"
        if k in ("link", "miniprogram", "note", "transfer"):
            return f"[{t}] {s}: [{k}] {it.get('title', '')} {it.get('url', '')}"
        return f"[{t}] {s}: [{k}] {it.get('text', '') or it.get('title', '')}"

    # ---------- 主导出 ----------
    def export(self, username: str, outdir: str, since_id: int = 0, with_media=True):
        os.makedirs(outdir, exist_ok=True)
        tables = self._msg_tables(username)
        if not tables:
            raise RuntimeError(f"未找到会话 {username} 的消息表")
        items, times, svrs = [], {}, {}
        for rel, table in tables:
            id2name, contact, room_members, owner, announcement, display = self._load_identities(username, rel)
            con = self.snap.connect(rel)
            is_group = username.endswith("@chatroom")
            rows = con.execute(
                f'select local_id, local_type, real_sender_id, create_time, message_content, server_id, '
                f'packed_info_data from "{table}" where local_id > ? order by local_id', (since_id,)
            ).fetchall()
            for lid, ltype, sid, ctime, mc, svr, packed in rows:
                raw = zdec(mc)
                it = self._parse_message(lid, ltype, sid, ctime, raw, id2name, display, is_group, packed=packed)
                items.append(it)
                times[lid] = ctime
                if svr:
                    svrs[lid] = svr
            self._meta = {"owner": owner, "announcement": announcement,
                          "display": display, "room_members": room_members}
        media_dir = os.path.join(outdir, "media")
        report = None
        if with_media:
            report = self.fetch_media(username, items, times, media_dir, svrs=svrs)
        # 落盘（增量模式下与已有 messages.json 合并）
        json_path = os.path.join(outdir, "messages.json")
        if since_id > 0 and os.path.exists(json_path):
            try:
                old = json.load(open(json_path, encoding="utf-8"))
                seen_ids = {it["local_id"] for it in items}
                old_items = [it for it in old if it["local_id"] not in seen_ids]
                for it in old_items:
                    if it.get("media_file") and not it.get("media_path"):
                        it["media_path"] = os.path.abspath(os.path.join(outdir, "media", it["media_file"]))
                items = old_items + items
            except (OSError, ValueError):
                pass
        items.sort(key=lambda it: it["local_id"])
        with io.open(json_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        # 全量重导时重写文本日志，避免追加模式造成历史重复
        with io.open(os.path.join(outdir, "messages.txt"), "w" if since_id == 0 else "a", encoding="utf-8") as f:
            if since_id == 0:
                if self._meta.get("announcement"):
                    f.write("===== 群公告 =====\n" + self._meta["announcement"] + "\n\n")
                f.write(f"===== 群主: {self._meta['display'](self._meta['owner'])} =====\n\n")
            for it in items:
                f.write(self.render_line(it) + "\n")
        if report:
            with io.open(os.path.join(outdir, "media_report.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            # 媒体路径索引：local_id -> 绝对路径（导出文件或微信侧原始文件），供外部程序定位
            index = {}
            for it in items:
                if it["kind"] in ("image", "video", "file", "voice", "emoji"):
                    index[str(it["local_id"])] = {
                        "kind": it["kind"], "time": it["time"], "sender": it["sender"],
                        "media_path": it.get("media_path", ""),
                        "source_path": it.get("source_path", ""),
                    }
            with io.open(os.path.join(outdir, "media_index.json"), "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=1)
        return items, report


    # ---------- 待下载提醒 ----------

    @staticmethod
    def remind_pending(out_root: str):
        """扫描所有会话：对方发来但尚未落盘的文件（文件名来自消息 XML，无需下载即可见）。

        终端输出提醒清单，并写 needs_download.json 供后续处理（人工下载/自动化）。
        返回：{chat: [item, ...]} 仅含未落盘的文件消息。
        """
        pending = {}
        for mj in glob.glob(os.path.join(out_root, "*", "messages.json")):
            chat = os.path.basename(os.path.dirname(mj))
            try:
                msgs = json.load(open(mj, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            misses = []
            for it in msgs:
                if it["kind"] != "file":
                    continue
                if it.get("media_file") or it.get("media_path"):
                    continue  # 已落盘
                misses.append({
                    "chat": chat,
                    "local_id": it["local_id"],
                    "time": it["time"],
                    "sender": it["sender"],
                    "filename": (it.get("title") or "").strip(),
                    "wechat_path": it.get("source_path", ""),
                })
            if misses:
                pending[chat] = misses
        n = sum(len(v) for v in pending.values())
        print(f"待下载文件 {n} 个（涉及 {len(pending)} 个会话）：")
        for chat, misses in pending.items():
            print(f"▸ {chat}")
            for it in misses:
                print(f"  #{it['local_id']} [{it['time']}] {it['sender']}: {it['filename'] or '(文件名未知)'}")
        with io.open(os.path.join(out_root, "needs_download.json"), "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=1)
        print(f"清单已写入 {os.path.join(out_root, 'needs_download.json')}")
        return pending

    # ---------- 语音转录 ----------

    def transcribe_chat(self, outdir: str, lids=None, force=False):
        """对已导出的语音补齐文字转录：微信原生转写已在导出时提取，这里走豆包 ASR 补缺。

        密钥读 data/workbench_settings.json 的 voice.app_id / voice.access_token（与工作台共用）。
        逐条写回 messages.json，可中断续跑；--force 重转已有转录。
        """
        from collections.abc import Mapping as _Mapping
        mj = os.path.join(outdir, "messages.json")
        if not os.path.exists(mj):
            raise RuntimeError(f"未找到 {mj}，请先导出该会话")
        msgs = json.load(open(mj, encoding="utf-8"))
        settings_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data", "workbench_settings.json")
        try:
            voice_cfg = (json.load(open(settings_path, encoding="utf-8")).get("voice") or {})
        except (OSError, ValueError):
            voice_cfg = {}
        app_id = str(voice_cfg.get("app_id") or "").strip()
        token = str(voice_cfg.get("access_token") or "").strip()
        if not app_id or not token:
            print("豆包 ASR 未配置：请在 data/workbench_settings.json 的 voice 中填写 app_id/access_token")
            return 0
        media_dir = os.path.join(outdir, "media")
        client = DoubaoASRClient(app_id=app_id, access_token=token)
        done = 0
        for it in msgs:
            if it["kind"] != "voice":
                continue
            if lids and it["local_id"] not in lids:
                continue
            if it.get("transcript") and not force:
                continue
            fname = it.get("media_file") or ""
            audio = os.path.join(media_dir, fname) if fname else it.get("media_path", "")
            if not audio or not os.path.isfile(audio):
                print(f"  #{it['local_id']} 无本地音频，跳过（先重导出补语音）")
                continue
            try:
                if audio.lower().endswith(".silk"):
                    wav = os.path.splitext(audio)[0] + ".wav"
                    if self._silk_to_wav(audio, wav):
                        audio = wav
                    else:
                        print(f"  #{it['local_id']} SILK 转 WAV 失败，跳过")
                        continue
                with open(audio, "rb") as f:
                    wav_bytes = f.read()
                result = client.transcribe(wav_bytes, audio_format="wav", uid="wechat-catchcatch")
                it["transcript"] = result.text
                it["transcript_source"] = "doubao_asr_v2"
                if result.duration_ms:
                    it["transcript_duration_ms"] = result.duration_ms
                confidences = []
                for u in result.utterances:
                    if isinstance(u, _Mapping):
                        try:
                            confidences.append(float(u.get("confidence")))
                        except (TypeError, ValueError):
                            pass
                if confidences:
                    it["transcript_confidence"] = round(sum(confidences) / len(confidences), 4)
                done += 1
                json.dump(msgs, open(mj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                print(f"  #{it['local_id']} {it['sender']}: {result.text[:50]}")
            except Exception as e:  # noqa: BLE001
                print(f"  #{it['local_id']} 转录失败: {str(e)[:80]}")
        print(f"本轮豆包转录 {done} 条")
        return done


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="微信会话全量导出（纯后台）")
    ap.add_argument("--chat", help="会话名（模糊匹配）")
    ap.add_argument("--list", action="store_true", help="列出所有群")
    ap.add_argument("--out", default=BASE_OUT)
    ap.add_argument("--no-media", action="store_true")
    ap.add_argument("--watch", type=int, default=0, metavar="秒", help="后台轮询增量抓取间隔")
    ap.add_argument("--locate", action="store_true", help="列出该会话所有媒体文件的绝对路径（不导出）")
    ap.add_argument("--lid", type=int, help="配合 --locate：只看指定 local_id")
    ap.add_argument("--missing", action="store_true", help="配合 --locate：只看缺失的媒体")
    ap.add_argument("--summary", action="store_true", help="汇总所有已导出会话的媒体覆盖率")
    ap.add_argument("--remind", action="store_true", help="列出对方发来、尚未下载的文件（含文件名），生成 needs_download.json")
    ap.add_argument("--asr", action="store_true", help="导出后对语音做豆包 ASR 转录（需在 workbench_settings.json 配置密钥）")
    ap.add_argument("--transcribe", action="store_true", help="仅对已有导出做语音转录，不重新导出")
    ap.add_argument("--force", action="store_true", help="重转已有转录的语音")
    args = ap.parse_args()

    ex = ChatExporter(args.out)

    if args.summary:
        import glob
        from collections import Counter
        reports = sorted(glob.glob(os.path.join(args.out, "*", "media_report.json")))
        if not reports:
            print("尚无导出数据（output 下没有 media_report.json）")
            return
        for rp in reports:
            try:
                r = json.load(open(rp, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            chat = os.path.basename(os.path.dirname(rp))
            cov = {k: f"{v[0]}/{v[1]}" for k, v in r.items() if k != "missing" and isinstance(v, list) and len(v) == 2}
            reasons = Counter(str(m.get("reason", "?")).split("(")[0] for m in r.get("missing", []))
            top = ",".join(f"{k}x{v}" for k, v in reasons.most_common(3)) or "-"
            print(f"{chat[:20]:<22} 图{cov.get('image','-'):>10} 视{cov.get('video','-'):>8} "
                  f"文{cov.get('file','-'):>6} 语{cov.get('voice','-'):>8}  缺失: {top}")
        return

    if args.remind:
        ChatExporter.remind_pending(args.out)
        return

    if args.list:
        hits = ex.find_chats("")
        for h in hits:
            print(h["name"] or h["username"], "|", h["remark"])
        return

    if not args.chat:
        ap.error("需要 --chat 或 --list")

    hits = ex.find_chats(args.chat)
    if not hits:
        print(f"未找到匹配「{args.chat}」的群聊")
        return
    target = hits[0]
    print(f"目标会话: {target['name']} ({target['username']})", flush=True)

    outdir = os.path.join(args.out, re.sub(r'[\\/:*?"<>|]', "_", target["name"] or target["username"]))

    if args.locate:
        mj = os.path.join(outdir, "messages.json")
        if not os.path.exists(mj):
            print("该会话还没有导出过（messages.json 不存在），请先运行一次导出")
            return
        msgs = json.load(open(mj, encoding="utf-8"))
        media_dir = os.path.join(outdir, "media")
        n = 0
        for it in msgs:
            if it["kind"] not in ("image", "video", "file", "voice", "emoji"):
                continue
            if args.lid and it["local_id"] != args.lid:
                continue
            p = it.get("media_path") or (os.path.join(media_dir, it["media_file"]) if it.get("media_file") else "")
            status = "OK" if p and os.path.isfile(p) else "缺失"
            if args.missing and status == "OK":
                continue
            src = it.get("source_path", "")
            line = f"#{it['local_id']} [{it['time']}] {it['sender']} {it['kind']} → {p or '(无文件)'} [{status}]"
            if status == "缺失" and src:
                line += f"  微信侧原始位置: {src}"
            if it.get("transcript"):
                line += f"\n        ↳ 转写({it.get('transcript_source', '?')}): {it['transcript'][:60]}"
            print(line)
            n += 1
        print(f"共 {n} 条媒体消息")
        return

    if args.transcribe:
        ex.transcribe_chat(outdir, force=args.force)
        return
    state_file = os.path.join(outdir, "state.json")
    since = 0
    if args.watch and os.path.exists(state_file):
        since = json.load(open(state_file)).get("last_local_id", 0)

    def run_once(since_id):
        items, report = ex.export(target["username"], outdir, since_id=since_id,
                                  with_media=not args.no_media)
        if items:
            last = max(it["local_id"] for it in items)
            json.dump({"last_local_id": last}, open(state_file, "w"))
            print(f"[{datetime.now():%H:%M:%S}] 新增 {len(items)} 条", flush=True)
        if report:
            cov = {k: f"{v[0]}/{v[1]}" for k, v in report.items() if k != "missing"}
            if report["missing"]:
                print("  媒体覆盖:", cov, "缺失:", len(report["missing"]), flush=True)
            else:
                print("  媒体覆盖:", cov, flush=True)
        return max((it["local_id"] for it in items), default=since_id)

    since = run_once(since)
    if args.asr:
        ex.transcribe_chat(outdir, force=args.force)
    print(f"输出目录: {outdir}", flush=True)
    if args.watch:
        print(f"进入后台监控，每 {args.watch}s 轮询（Ctrl+C 停止）", flush=True)
        while True:
            time.sleep(args.watch)
            try:
                ex.snap._cache.clear()  # 强制重新快照（WAL 增量合并）
                since = run_once(since)
            except Exception as e:  # noqa: BLE001
                print(f"[watch] 错误（继续）: {str(e)[:100]}", flush=True)
                time.sleep(10)


if __name__ == "__main__":
    main()
