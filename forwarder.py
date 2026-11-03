"""WhatsApp batch forwarder: drives an Android phone over ADB, no AI.

Opens the source chat, taps the forward button on the latest forwardable
message, ticks up to N contacts that appear BELOW the anchor contact in the
"Forward to..." list, verifies the recipient bar, sends, and logs the names.
"""
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_adb():
    p = shutil.which("adb")
    if p:
        return p
    guess = os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")
    return guess if os.path.exists(guess) else "adb"


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


class Stopped(Exception):
    pass


class CallInterrupted(Exception):
    """A call happened mid-step; the screen state is unknown, so redo the step."""


class Forwarder:
    def __init__(self, cfg, log_path, say, stop_event):
        self.cfg = cfg
        self.log_path = log_path
        self.say = say
        self.stop = stop_event
        self.adb_path = cfg.get("adb") or find_adb()
        self.device = cfg.get("device", "").strip()
        self.tmp = os.path.join(os.path.dirname(log_path), "ui.xml")

    # ---------- adb helpers ----------
    def adb(self, *args, timeout=30):
        cmd = [self.adb_path] + (["-s", self.device] if self.device else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout + r.stderr

    def connect(self):
        if self.device and ":" in self.device:
            out = self.adb_raw("connect", self.device)
            self.say(out.strip())
        out = self.adb_raw("devices")
        lines = [l for l in out.splitlines()[1:] if l.strip().endswith("device")]
        return lines

    def adb_raw(self, *args):
        r = subprocess.run([self.adb_path] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=NO_WINDOW)
        return r.stdout + r.stderr

    def sleep(self, s):
        if self.stop.wait(s):
            raise Stopped()

    def in_call(self):
        """True when input could touch a call: phone ringing (mCallState 1), or on a
        call (2) while something other than WhatsApp (e.g. the call screen) is in front."""
        states = re.findall(r"mCallState=(\d)", self.adb("shell", "dumpsys", "telephony.registry"))
        if "1" in states:
            return True
        if "2" in states:
            pkg = self.cfg.get("app_package") or "com.whatsapp.w4b"
            focus = re.search(r"mCurrentFocus=.*", self.adb("shell", "dumpsys", "window"))
            return not (focus and pkg + "/" in focus.group(0))
        return False

    def wait_if_call(self):
        """Never send input during a call: a swipe could answer or a tap could hang up."""
        if not self.in_call():
            return
        self.say("📞 Phone call in progress, paused. Will continue after the call ends.")
        while self.in_call():
            self.sleep(3)
        self.say("Call ended, continuing in 5 seconds…")
        self.sleep(5)
        raise CallInterrupted()

    def input(self, *args):
        self.wait_if_call()
        self.adb("shell", "input", *args)

    def tap(self, x, y):
        self.input("tap", str(x), str(y))

    def back(self):
        self.input("keyevent", "4")

    def swipe_up(self):
        self.input("swipe", "540", "1900", "540", "1000", "400")

    def dump(self):
        for _ in range(3):
            self.adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
            self.adb("pull", "/sdcard/ui.xml", self.tmp)
            try:
                return ET.parse(self.tmp).getroot()
            except (ET.ParseError, FileNotFoundError):
                self.sleep(1)
        raise RuntimeError("Could not read the phone screen (uiautomator dump failed).")

    # ---------- node helpers ----------
    @staticmethod
    def bounds(n):
        return list(map(int, re.findall(r"\d+", n.get("bounds", "0,0,0,0"))))

    def center(self, n):
        v = self.bounds(n)
        return (v[0] + v[2]) // 2, (v[1] + v[3]) // 2

    @staticmethod
    def by_rid(root, suffix):
        return [n for n in root.iter("node") if n.get("resource-id", "").endswith(suffix)]

    @staticmethod
    def by_text(root, text):
        return [n for n in root.iter("node") if n.get("text") == text]

    # ---------- log ----------
    def sent(self):
        try:
            with open(self.log_path, encoding="utf-8-sig") as f:
                return {l.strip() for l in f if l.strip()}
        except FileNotFoundError:
            return set()

    def record(self, names):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(names) + "\n")

    # ---------- screens ----------
    def open_source_chat(self):
        """Make sure the source chat is open; returns the forward button node."""
        src = self.cfg["source_chat"].strip()
        pkg = self.cfg.get("app_package") or "com.whatsapp.w4b"
        for attempt in range(8):
            root = self.dump()
            first = next(root.iter("node"), None)
            if first is None or first.get("package") != pkg:
                self.wait_if_call(); self.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
                self.sleep(3); continue
            if self.by_text(root, "Forward to…") or self.by_text(root, "Forward to..."):
                self.back(); self.sleep(1.5); continue
            title = self.by_rid(root, "conversation_contact_name")
            if title:
                if title[0].get("text", "").strip() == src:
                    fws = [n for n in root.iter("node")
                           if re.fullmatch(r"forward( \w+)?", n.get("content-desc", "").lower())]
                    if fws:
                        return max(fws, key=lambda n: self.bounds(n)[1])  # latest message
                    self.swipe_down_chat(); self.sleep(1); continue
                self.back(); self.sleep(1.5); continue  # some other chat (or its keyboard)
            rows = self.by_rid(root, "conversations_row_contact_name")
            if rows:
                match = [n for n in rows if n.get("text", "").strip() == src]
                if match:
                    self.tap(*self.center(match[0])); self.sleep(2)
                else:  # scroll chat list back to the top, never press back here
                    self.input("swipe", "540", "900", "540", "2000", "300"); self.sleep(1)
                continue
            self.back(); self.sleep(1.5)
        raise RuntimeError(f'Could not open chat "{src}" with a forward button. '
                           "Open that chat on the phone, scroll so the message is visible, then Start again.")

    def swipe_down_chat(self):
        # toward the newest messages
        self.input("swipe", "540", "1900", "540", "1500", "300")

    def picker_rows(self, root):
        rows = []
        for n in self.by_rid(root, "contactpicker_row_container"):
            name = status = None
            for c in n.iter("node"):
                rid = c.get("resource-id", "")
                if rid.endswith("contactpicker_row_name"):
                    name = c.get("text")
                elif rid.endswith("contactpicker_row_status"):
                    status = c.get("text")
            v = self.bounds(n)
            # only rows fully on screen (not under toolbar / message preview bar)
            if name and v[3] - v[1] > 120 and v[1] >= 264 and v[3] <= 2200:
                rows.append((v[1], name, status or "", n.get("checked") == "true", self.center(n)))
        return sorted(rows)

    # ---------- one batch ----------
    def batch(self):
        cfg = self.cfg
        anchor = norm(cfg["anchor"])
        excl = {norm(x) for x in cfg["exclude"] if x.strip()}
        size = int(cfg["batch_size"])
        fw = self.open_source_chat()
        self.tap(*self.center(fw)); self.sleep(2.5)

        done = self.sent()
        picked, seen = [], set()
        below = not anchor          # no anchor -> start from top
        stale = 0
        while len(picked) < size and stale < 2:
            new = 0
            for _, name, status, chk, c in self.picker_rows(self.dump()):
                if name in seen:
                    continue
                seen.add(name); new += 1
                if anchor and norm(name).startswith(anchor):
                    below = True; continue
                if not below or chk or name in done or name in picked:
                    continue
                if norm(name) in excl or name in ("My status", "New business broadcast"):
                    continue
                if cfg["skip_groups"] and status.endswith(", You"):
                    continue
                self.tap(*c); self.sleep(0.5); picked.append(name)
                if len(picked) == size:
                    break
            if len(picked) < size:
                stale = stale + 1 if new == 0 else 0
                self.swipe_up(); self.sleep(1)

        if not below:
            self.back()
            raise RuntimeError(f'Start contact "{cfg["anchor"]}" was not found in the Forward list. Nothing sent.')
        if not picked:
            self.back()
            return None  # finished

        root = self.dump()
        rec = self.by_rid(root, "recipients")
        send = self.by_rid(root, "send")
        rtxt = rec[0].get("text", "") if rec else ""
        names_in_bar = [x.strip() for x in rtxt.split(", ") if x.strip()]
        picked = [p for p in picked if p in rtxt]
        if (not send or not picked or len(names_in_bar) != len(picked)
                or any(anchor and norm(x).startswith(anchor) for x in names_in_bar)):
            self.back()
            raise RuntimeError(f"Recipient check failed, NOT sent. Bar showed: {rtxt}")
        self.tap(*self.center(send[0])); self.sleep(3)
        self.record(picked)
        return picked

    def run(self):
        n = 0
        try:
            while not self.stop.is_set():
                try:
                    picked = self.batch()
                except CallInterrupted:
                    continue  # redo this batch from a clean screen
                if picked is None:
                    self.say("✅ No more contacts left below the start contact. All done.")
                    break
                n += 1
                self.say(f"Batch {n} sent ({len(picked)}): " + " | ".join(picked), sent=len(picked))
                self.sleep(float(self.cfg["delay"]))
        except Stopped:
            self.say("⏹ Stopped by you.")
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ {e}")
