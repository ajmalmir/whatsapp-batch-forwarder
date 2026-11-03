"""Collect a WhatsApp group's unsaved member numbers over ADB, no AI.

Group info -> Search members gives the full member list. Only members NOT
saved in the phone are collected: they show as their number in the list.
Saved contacts (shown by name) are skipped, so no member is ever tapped.
Each number is written to the app's own numbers.csv the moment it is found.
"""
import csv
import os
import re
import time

from forwarder import CallInterrupted, Forwarder, Stopped

PHONE = re.compile(r"^\+?\d[\d\s\-()]{6,}\d$")
SKIP_ROWS = {"You", "Add members", "Invite to group via link", "View member changes", "View all"}
FIELDS = ["Number", "Contact name", "WhatsApp name", "Group", "Saved at"]


def digits(number):
    return re.sub(r"\D", "", number)


class NumberStore:
    """numbers.csv inside the app folder: one row per unique number, across all groups."""

    def __init__(self, path):
        self.path = path
        self.known = {digits(r["Number"]) for r in self.rows()}

    def rows(self):
        try:
            with open(self.path, newline="", encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))
        except FileNotFoundError:
            return []

    def add(self, number, push, group):
        """Append immediately (so nothing is lost if stopped). Returns False if already saved."""
        key = digits(number)
        if key in self.known:
            return False
        new_file = not os.path.exists(self.path)
        # WhatsApp hides unsaved members' names in the member list, so number them per group
        contact = f"{group} - {push}" if push else f"{group} {self.groups().get(group, 0) + 1:03d}"
        with open(self.path, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({"Number": number, "Contact name": contact,
                        "WhatsApp name": push, "Group": group, "Saved at": time.strftime("%Y-%m-%d %H:%M")})
        self.known.add(key)
        return True

    def groups(self):
        out = {}
        for r in self.rows():
            out[r["Group"]] = out.get(r["Group"], 0) + 1
        return out

    def export(self, dest, group=None):
        rows = [r for r in self.rows() if not group or r["Group"] == group]
        with open(dest, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig so Excel shows names right
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        return len(rows)

    def clear(self, backup):
        if os.path.exists(self.path):
            os.replace(self.path, backup)
        self.known = set()


class Extractor(Forwarder):
    def open_chat(self, name):
        """Open any chat by exact name from the chat list (scrolls to find it)."""
        pkg = self.cfg.get("app_package") or "com.whatsapp.w4b"
        swipes, at_top = 0, False
        for _ in range(60):
            root = self.dump()
            first = next(root.iter("node"), None)
            if first is None or first.get("package") != pkg:
                self.say("  WhatsApp not in front, opening it…")
                self.wait_if_call(); self.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
                self.sleep(3); continue
            if any(self.by_rid(root, r) for r in ("search_src_text", "group_details_card_subtitle",
                                                    "participants_title")):
                self.say("  On a group info / member list screen, going back…")
                self.back(); self.sleep(1.2); continue
            title = self.by_rid(root, "conversation_contact_name")
            # match on the title only: admin-only groups have no message box ("entry")
            if title and title[0].get("text", "").strip() == name:
                self.say("  Group chat open.")
                return
            rows = self.by_rid(root, "conversations_row_contact_name")
            if not rows:
                self.say("  Going back to the chat list…")
                self.back(); self.sleep(1.2); continue
            hit = [n for n in rows if n.get("text", "").strip() == name]
            if hit:
                self.say("  Found it in the chat list, opening…")
                self.tap(*self.center(hit[0])); self.sleep(2); continue
            if not at_top:   # go to the top of the chat list first, then scroll down looking for it
                for _ in range(6):
                    self.input("swipe", "540", "900", "540", "2000", "200")
                self.sleep(0.8); at_top = True; continue
            swipes += 1
            self.say(f"  Searching the chat list… (page {swipes})")
            self.swipe_up(); self.sleep(1)
            if swipes > 40:
                break
        raise RuntimeError(f'Could not find a chat named "{name}". Check the exact group name.')

    def after_marker(self, root, rid_suffix):
        """Nodes that come after the given node in tree order (the overlay layer)."""
        out, seen = [], False
        for n in root.iter("node"):
            if seen:
                out.append(n)
            elif n.get("resource-id", "").endswith(rid_suffix):
                seen = True
        return out

    def member_rows(self, root):
        """(name_text, number, whatsapp_name) per visible member row.

        Unsaved members appear either as just their number, or as "~ Name" with the
        number beside it (secondary_name_view_text). Saved contacts show a name only.
        """
        rows = []
        for n in self.after_marker(root, "search_src_text"):
            rid = n.get("resource-id", "")
            t = (n.get("text") or "").strip()
            if rid.endswith("/name"):
                v = self.bounds(n)
                if t and t not in SKIP_ROWS and v[1] >= 270 and v[3] <= 2330:
                    rows.append({"y": v[1], "name": t, "number": ""})
            elif rid.endswith("secondary_name_view_text") and rows and PHONE.match(t):
                if abs(self.bounds(n)[1] - rows[-1]["y"]) < 60:   # same row as the last name
                    rows[-1]["number"] = t
        out = []
        for r in rows:
            name = r["name"]
            if PHONE.match(name):
                out.append((name, name, ""))
            else:
                push = name.lstrip("~").strip("   ") if name.startswith("~") else ""
                if not re.search(r"\w", push):   # names like "," or "." are useless
                    push = ""
                out.append((name, r["number"], push))
        return out

    def extract(self, group):
        self.say(f'Step 1/4 · Opening group "{group}"…')
        self.open_chat(group)

        self.say("Step 2/4 · Opening group info…")
        t = self.by_rid(self.dump(), "conversation_contact_name")[0]
        self.tap(*self.center(t)); self.sleep(2.5)
        for _ in range(6):
            root = self.dump()
            s = self.by_rid(root, "participants_search")
            if s:
                break
            self.swipe_up(); self.sleep(1)
        else:
            raise RuntimeError("No member list on the group info page. If this is a Community announcement "
                               "group, WhatsApp hides members from non-admins, so numbers can't be read.")
        sub = self.by_rid(root, "group_details_card_subtitle")
        total = sub[0].get("text", "").replace("Group · ", "") if sub else "?"
        self.say(f"  Group has {total}.")

        self.say("Step 3/4 · Opening the full member list…")
        self.tap(*self.center(s[0])); self.sleep(2)
        self.input("keyevent", "111"); self.sleep(0.8)   # hide keyboard

        self.say("Step 4/4 · Reading members (saved contacts are skipped)…")
        seen, stale, page = self.seen, 0, 0
        while stale < 2:
            page += 1
            new = 0
            for name, number, push in self.member_rows(self.dump()):
                key = number or name
                if key in seen:
                    continue
                if name.startswith("~") and not number:
                    continue   # number not on screen yet (row cut off) - pick it up next page
                seen.add(key); new += 1
                if not number:   # name only = saved in your phone -> skip
                    self.skipped += 1
                    self.say(f"  – {name} (saved in phone, skipped)")
                    continue
                if self.store.add(number, push, group):
                    self.added += 1
                    self.say(f"  ✚ {number}  {push}".rstrip(), sent=1)
                else:
                    self.dupes += 1
                    self.say(f"  = {number} already in your list")
            self.say(f"  Page {page}: {len(seen)} members checked · {self.added} new numbers so far")
            stale = stale + 1 if new == 0 else 0
            self.input("swipe", "540", "1900", "540", "900", "400"); self.sleep(1)

        self.say("  End of member list, going back…")
        self.back(); self.sleep(1); self.back(); self.sleep(1)

    def run_extract(self, group, store):
        self.store, self.seen = store, set()
        self.added = self.dupes = self.skipped = 0
        try:
            for _ in range(10):
                try:
                    self.extract(group)
                    break
                except CallInterrupted:
                    self.say(f"Resuming after call ({len(self.seen)} members already checked)…")
            else:
                raise RuntimeError("Too many interruptions, stopped.")
            self.say(f"✅ Finished \"{group}\": {self.added} new numbers saved · {self.dupes} already in list · "
                     f"{self.skipped} saved contacts skipped.")
        except Stopped:
            self.say(f"⏹ Stopped by you. {self.added} numbers found so far are saved.")
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ {e}  ({self.added} numbers found before this are saved.)")
