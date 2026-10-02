#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OTOPARCA_DEPO_PRO — Oto yedek parça işletmeleri için depo ve stok yönetimi.

Özellikler: OEM numarası / araç uyumluluğu ile parça arama, raf adresleme, mal kabul (giriş),
            satış/servis çıkışı, iade, ağırlıklı ortalama maliyet, depo sayımı ve fark raporu,
            kritik stok ve sipariş önerisi, barkodlu raf etiketi (Code 128), CSV içe/dışa aktarma,
            rol tabanlı yetkilendirme ve işlem kaydı.
Teknoloji : Python 3, tkinter/ttk, SQLite (standart kütüphane dışında bağımlılık yok)

Kullanım:
    python otoparca_depo_pro.py            # uygulamayı açar
    python otoparca_depo_pro.py --demo     # örnek verilerle açar (ilk çalıştırmada)
    python otoparca_depo_pro.py --test     # birim testlerini çalıştırır
    python otoparca_depo_pro.py --db yol   # farklı veritabanı dosyası kullanır

Geliştirici: Enes Talha Köse (github.com/enestalhakose)
"""

import argparse
import csv
import hashlib
import hmac
import html
import os
import re
import secrets
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

APP_NAME = "OTOPARCA DEPO PRO"
VERSION = "1.0.0"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "otoparca_depo.db")

PBKDF2_ROUNDS = 120_000
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 5

ROLE_PERMS = {
    "yonetici": {"*"},
    "depocu": {"parca.yaz", "giris", "iade", "sayim", "etiket"},
    "tezgah": {"cikis", "iade"},
}
ROLE_NAMES = {"yonetici": "Yönetici", "depocu": "Depo sorumlusu", "tezgah": "Tezgâh / satış"}
DOC_TYPES = {"GIRIS": "Mal kabul", "CIKIS": "Satış / servis çıkışı", "IADE": "Müşteri iadesi"}
DOC_PREFIX = {"GIRIS": "GF", "CIKIS": "CF", "IADE": "IF"}
DOC_PERM = {"GIRIS": "giris", "CIKIS": "cikis", "IADE": "iade"}
DOC_SIGN = {"GIRIS": 1, "CIKIS": -1, "IADE": 1}
CATEGORIES = ["Fren", "Filtre", "Motor", "Süspansiyon", "Elektrik", "Şanzıman", "Soğutma", "Kaporta",
              "Aydınlatma", "Yağ ve sıvılar", "Diğer"]
CSV_HEADERS = ["OEM", "Marka", "Parça adı", "Kategori", "Raf", "Stok", "Kritik", "Alış", "Satış", "Uyumlu araçlar"]


# ════════════════════════════════════════════════════════════════════
#  Yardımcılar
# ════════════════════════════════════════════════════════════════════
class DepoError(Exception):
    """Kullanıcıya gösterilecek iş kuralı hatası."""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm_oem(text) -> str:
    """'7701 208 265' / '77-01-208265' -> '7701208265'. Aramada yazım farklarını yok sayar."""
    return re.sub(r"[^0-9A-Z]", "", str(text).upper())


def parse_money(text) -> int:
    s = str(text).strip().replace("₺", "").replace("TL", "").replace(" ", "")
    if not s:
        raise DepoError("Tutar boş olamaz.")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise DepoError(f"Geçersiz tutar: {text}")
    if d < 0:
        raise DepoError("Tutar negatif olamaz.")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_money(kurus) -> str:
    kurus = int(kurus or 0)
    lira, kr = divmod(abs(kurus), 100)
    return f"{'-' if kurus < 0 else ''}{lira:,}".replace(",", ".") + f",{kr:02d} ₺"


def parse_int(text, name="Miktar", allow_zero=False, allow_negative=False) -> int:
    try:
        v = int(str(text).strip())
    except ValueError:
        raise DepoError(f"{name} tam sayı olmalı.")
    if v < 0 and not allow_negative:
        raise DepoError(f"{name} negatif olamaz.")
    if v == 0 and not allow_zero:
        raise DepoError(f"{name} sıfır olamaz.")
    return v


def norm_location(text) -> str:
    """Raf adresi biçimi: KORIDOR-RAF-GÖZ, ör. 'a 3 2' -> 'A-03-2'."""
    parts = [p for p in re.split(r"[\s\-_/.]+", str(text).strip().upper()) if p]
    if not parts:
        return ""
    if len(parts) != 3 or not parts[0].isalpha() or not parts[1].isdigit() or not parts[2].isdigit():
        raise DepoError("Raf adresi KORİDOR-RAF-GÖZ biçiminde olmalı (ör. A-03-2).")
    return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2])}"


def parse_fitments(text):
    """Her satır: 'Marka | Model | 2012-2019' (yıl aralığı isteğe bağlı)."""
    out = []
    for i, line in enumerate(str(text).splitlines(), start=1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise DepoError(f"Uyumlu araç satırı {i} hatalı. Biçim: Marka | Model | 2012-2019")
        y1, y2 = 1950, 2100
        if len(parts) > 2 and parts[2]:
            m = re.fullmatch(r"(\d{4})\s*(?:-\s*(\d{4})?)?", parts[2])
            if not m:
                raise DepoError(f"Uyumlu araç satırı {i}: yıl '2012-2019' veya '2015' biçiminde olmalı.")
            y1 = int(m.group(1))
            y2 = int(m.group(2)) if m.group(2) else (2100 if "-" in parts[2] else y1)
            if y2 < y1:
                raise DepoError(f"Uyumlu araç satırı {i}: bitiş yılı başlangıçtan küçük.")
        out.append((parts[0].title(), parts[1], y1, y2))
    return out


def fmt_fitments(rows):
    lines = []
    for r in rows:
        y = "" if (r["year_from"], r["year_to"]) == (1950, 2100) else \
            f"{r['year_from']}-" if r["year_to"] == 2100 else \
            f"{r['year_from']}" if r["year_from"] == r["year_to"] else f"{r['year_from']}-{r['year_to']}"
        lines.append(f"{r['make']} | {r['model']}" + (f" | {y}" if y else ""))
    return "\n".join(lines)


def hash_password(password: str, salt: bytes = None):
    salt = salt or secrets.token_bytes(16)
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex(), salt.hex()


def check_password_policy(pw: str):
    if len(pw) < 8 or not any(c.isdigit() for c in pw) or not any(c.isalpha() for c in pw):
        raise DepoError("Şifre en az 8 karakter olmalı, harf ve rakam içermeli.")


def part_code(pid: int) -> str:
    """Barkoda basılan dahili parça kodu."""
    return f"P{pid:06d}"


# ── Code 128-B barkod (harici kütüphane olmadan SVG) ────────────────
CODE128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112").split()


def code128_widths(text: str):
    """Code 128-B kodlama: bar/boşluk genişlikleri listesi (başlangıç, veri, kontrol, bitiş)."""
    if not text or any(not 32 <= ord(c) <= 126 for c in text):
        raise DepoError("Barkod yalnızca standart ASCII karakter içerebilir.")
    codes = [104] + [ord(c) - 32 for c in text]
    check = (codes[0] + sum(i * c for i, c in enumerate(codes[1:], start=1))) % 103
    return [int(w) for c in codes + [check, 106] for w in CODE128[c]]


def code128_svg(text: str, module=2, height=56) -> str:
    x, bars = 10 * module, []
    for i, w in enumerate(code128_widths(text)):
        if i % 2 == 0:
            bars.append(f'<rect x="{x}" y="0" width="{w * module}" height="{height}"/>')
        x += w * module
    width = x + 10 * module
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#fff"/>'
            f'<g fill="#000">{"".join(bars)}</g></svg>')


@dataclass
class User:
    id: int
    username: str
    role: str
    must_change: bool = False


# ════════════════════════════════════════════════════════════════════
#  İş katmanı
# ════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, pw_hash TEXT NOT NULL,
    salt TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('yonetici','depocu','tezgah')),
    active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parts(
    id INTEGER PRIMARY KEY, oem TEXT NOT NULL, oem_norm TEXT NOT NULL, brand TEXT NOT NULL,
    name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Diğer', location TEXT NOT NULL DEFAULT '',
    stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0), min_stock INTEGER NOT NULL DEFAULT 0,
    avg_cost INTEGER NOT NULL DEFAULT 0, sell_price INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    UNIQUE(oem_norm, brand));
CREATE TABLE IF NOT EXISTS fitments(
    id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    make TEXT NOT NULL, model TEXT NOT NULL, year_from INTEGER NOT NULL, year_to INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS docs(
    id INTEGER PRIMARY KEY, no TEXT UNIQUE NOT NULL, type TEXT NOT NULL CHECK(type IN ('GIRIS','CIKIS','IADE')),
    date TEXT NOT NULL, party TEXT, plate TEXT, note TEXT, total INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER, status TEXT NOT NULL DEFAULT 'AKTIF' CHECK(status IN ('AKTIF','IPTAL')), cancel_reason TEXT);
CREATE TABLE IF NOT EXISTS doc_lines(
    id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES docs(id), part_id INTEGER NOT NULL REFERENCES parts(id),
    qty INTEGER NOT NULL CHECK(qty>0), unit_price INTEGER NOT NULL, cost_before INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS moves(
    id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id), date TEXT NOT NULL,
    qty INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT, user_id INTEGER);
CREATE TABLE IF NOT EXISTS counts(
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, scope TEXT, status TEXT NOT NULL DEFAULT 'ACIK'
    CHECK(status IN ('ACIK','UYGULANDI')), user_id INTEGER, applied TEXT);
CREATE TABLE IF NOT EXISTS count_lines(
    count_id INTEGER NOT NULL REFERENCES counts(id), part_id INTEGER NOT NULL REFERENCES parts(id),
    system_qty INTEGER NOT NULL, counted INTEGER, PRIMARY KEY(count_id, part_id));
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_parts_oem ON parts(oem_norm);
CREATE INDEX IF NOT EXISTS ix_fit_part ON fitments(part_id);
CREATE INDEX IF NOT EXISTS ix_fit_make ON fitments(make, model);
CREATE INDEX IF NOT EXISTS ix_moves_part ON moves(part_id, date);
"""


class Depo:
    def __init__(self, path=DEFAULT_DB):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            h, s = hash_password("admin123")
            self.db.execute("INSERT INTO users(username,pw_hash,salt,role,must_change,created_at) "
                            "VALUES('admin',?,?,'yonetici',1,?)", (h, s, now_str()))
            self.db.commit()

    # ── altyapı ──────────────────────────────────────────────────────
    @contextmanager
    def tx(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def q(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def q1(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def _audit(self, user, action, detail=""):
        self.db.execute("INSERT INTO audit(ts,username,action,detail) VALUES(?,?,?,?)",
                        (now_str(), user.username if user else "-", action, detail))

    def _move(self, user, part_id, qty, kind, ref="", date=None):
        self.db.execute("UPDATE parts SET stock=stock+? WHERE id=?", (qty, part_id))
        self.db.execute("INSERT INTO moves(part_id,date,qty,kind,ref,user_id) VALUES(?,?,?,?,?,?)",
                        (part_id, date or now_str(), qty, kind, ref, user.id))

    @staticmethod
    def can(user, perm):
        perms = ROLE_PERMS.get(user.role, set())
        return "*" in perms or perm in perms

    def _require(self, user, perm):
        if not self.can(user, perm):
            raise DepoError("Bu işlem için yetkiniz yok.")

    # ── kimlik doğrulama ve kullanıcılar ─────────────────────────────
    def login(self, username, password) -> User:
        row = self.q1("SELECT * FROM users WHERE username=?", (username.strip(),))
        if not row or not row["active"]:
            raise DepoError("Kullanıcı adı veya şifre hatalı.")
        if row["locked_until"] and row["locked_until"] > now_str():
            raise DepoError(f"Çok fazla hatalı deneme. Hesap {row['locked_until'][11:16]} saatine kadar kilitli.")
        ok = hmac.compare_digest(hash_password(password, bytes.fromhex(row["salt"]))[0], row["pw_hash"])
        with self.tx():
            if ok:
                self.db.execute("UPDATE users SET failed=0, locked_until=NULL WHERE id=?", (row["id"],))
                self.db.execute("INSERT INTO audit(ts,username,action) VALUES(?,?,'GIRIS')", (now_str(), row["username"]))
            else:
                failed, locked = row["failed"] + 1, None
                if failed >= MAX_FAILED_LOGINS:
                    failed, locked = 0, (datetime.now() + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                self.db.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (failed, locked, row["id"]))
                self.db.execute("INSERT INTO audit(ts,username,action) VALUES(?,?,'GIRIS_HATALI')",
                                (now_str(), row["username"]))
        if not ok:
            raise DepoError("Kullanıcı adı veya şifre hatalı.")
        return User(row["id"], row["username"], row["role"], bool(row["must_change"]))

    def change_password(self, user, old, new):
        row = self.q1("SELECT * FROM users WHERE id=?", (user.id,))
        if not hmac.compare_digest(hash_password(old, bytes.fromhex(row["salt"]))[0], row["pw_hash"]):
            raise DepoError("Mevcut şifre hatalı.")
        check_password_policy(new)
        if new == old:
            raise DepoError("Yeni şifre eskisiyle aynı olamaz.")
        h, s = hash_password(new)
        with self.tx():
            self.db.execute("UPDATE users SET pw_hash=?, salt=?, must_change=0 WHERE id=?", (h, s, user.id))
            self._audit(user, "SIFRE_DEGISTI")
        user.must_change = False

    def list_users(self):
        return self.q("SELECT id,username,role,active,created_at FROM users ORDER BY username")

    def add_user(self, user, username, password, role):
        self._require(user, "kullanici")
        username = username.strip()
        if len(username) < 3 or not username.replace("_", "").replace(".", "").isalnum():
            raise DepoError("Kullanıcı adı en az 3 karakter olmalı; harf, rakam, '.' ve '_' içerebilir.")
        if role not in ROLE_PERMS:
            raise DepoError("Geçersiz rol.")
        check_password_policy(password)
        h, s = hash_password(password)
        try:
            with self.tx():
                self.db.execute("INSERT INTO users(username,pw_hash,salt,role,must_change,created_at) "
                                "VALUES(?,?,?,?,1,?)", (username, h, s, role, now_str()))
                self._audit(user, "KULLANICI_EKLE", f"{username} ({role})")
        except sqlite3.IntegrityError:
            raise DepoError("Bu kullanıcı adı zaten var.")

    def set_user_active(self, user, user_id, active):
        self._require(user, "kullanici")
        if user_id == user.id:
            raise DepoError("Kendi hesabınızı pasifleştiremezsiniz.")
        with self.tx():
            self.db.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
            self._audit(user, "KULLANICI_DURUM", f"id={user_id} aktif={active}")

    def reset_password(self, user, user_id, new_password):
        self._require(user, "kullanici")
        check_password_policy(new_password)
        h, s = hash_password(new_password)
        with self.tx():
            self.db.execute("UPDATE users SET pw_hash=?, salt=?, must_change=1, failed=0, locked_until=NULL "
                            "WHERE id=?", (h, s, user_id))
            self._audit(user, "SIFRE_SIFIRLA", f"id={user_id}")

    # ── parçalar ─────────────────────────────────────────────────────
    def search_parts(self, text="", make="", model="", year=None, category="", only_active=True, limit=500):
        """OEM (yazım farkı gözetmeden), dahili barkod kodu, parça adı, marka veya araç ile arama."""
        sql = "SELECT DISTINCT p.* FROM parts p"
        where, params = [], []
        if make or model or year:
            sql += " JOIN fitments f ON f.part_id=p.id"
            if make:
                where.append("f.make LIKE ?")
                params.append(f"%{make.strip()}%")
            if model:
                where.append("f.model LIKE ?")
                params.append(f"%{model.strip()}%")
            if year:
                where.append("? BETWEEN f.year_from AND f.year_to")
                params.append(int(year))
        text = text.strip()
        if text:
            m = re.fullmatch(r"[Pp](\d{6})", text)
            if m:
                where.append("p.id=?")
                params.append(int(m.group(1)))
            else:
                n = norm_oem(text)
                like = f"%{text}%"
                where.append("(p.name LIKE ? OR p.brand LIKE ? OR p.location LIKE ?" +
                             (" OR p.oem_norm LIKE ?" if n else "") +
                             " OR EXISTS(SELECT 1 FROM fitments x WHERE x.part_id=p.id AND "
                             "(x.make || ' ' || x.model) LIKE ?))")
                params += [like, like, like] + ([f"%{n}%"] if n else []) + [like]
        if category:
            where.append("p.category=?")
            params.append(category)
        if only_active:
            where.append("p.active=1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.q(sql + " ORDER BY p.name LIMIT ?", params + [limit])

    def get_part(self, pid):
        return self.q1("SELECT * FROM parts WHERE id=?", (pid,))

    def fitments(self, pid):
        return self.q("SELECT * FROM fitments WHERE part_id=? ORDER BY make, model, year_from", (pid,))

    def makes(self):
        return [r["make"] for r in self.q("SELECT DISTINCT make FROM fitments ORDER BY make")]

    def _validate_part(self, oem, brand, name, category, location, min_stock, sell):
        oem, brand, name = oem.strip().upper(), brand.strip().upper(), name.strip()
        if not norm_oem(oem) or not brand or not name:
            raise DepoError("OEM numarası, parça markası ve parça adı zorunludur.")
        if category not in CATEGORIES:
            raise DepoError("Geçersiz kategori.")
        return dict(oem=oem, oem_norm=norm_oem(oem), brand=brand, name=name, category=category,
                    location=norm_location(location), min_stock=parse_int(min_stock, "Kritik stok", allow_zero=True),
                    sell_price=parse_money(sell))

    def save_part(self, user, pid, oem, brand, name, category, location, min_stock, sell, fitments_text="",
                  opening_stock=0, opening_cost="0", active=True):
        self._require(user, "parca.yaz")
        v = self._validate_part(oem, brand, name, category, location, min_stock, sell)
        fits = parse_fitments(fitments_text)
        try:
            with self.tx():
                if pid:
                    self.db.execute("UPDATE parts SET oem=?,oem_norm=?,brand=?,name=?,category=?,location=?,"
                                    "min_stock=?,sell_price=?,active=? WHERE id=?",
                                    (*v.values(), 1 if active else 0, pid))
                    self.db.execute("DELETE FROM fitments WHERE part_id=?", (pid,))
                    self._audit(user, "PARCA_GUNCELLE", f"{v['brand']} {v['oem']}")
                else:
                    opening = parse_int(opening_stock, "Açılış stoğu", allow_zero=True)
                    cost = parse_money(opening_cost)
                    pid = self.db.execute(
                        "INSERT INTO parts(oem,oem_norm,brand,name,category,location,min_stock,sell_price,avg_cost,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (*v.values(), cost, now_str())).lastrowid
                    if opening:
                        self._move(user, pid, opening, "ACILIS")
                    self._audit(user, "PARCA_EKLE", f"{v['brand']} {v['oem']}")
                self.db.executemany("INSERT INTO fitments(part_id,make,model,year_from,year_to) VALUES(?,?,?,?,?)",
                                    [(pid, *f) for f in fits])
                return pid
        except sqlite3.IntegrityError:
            raise DepoError(f"{v['brand']} markalı {v['oem']} OEM numaralı parça zaten kayıtlı.")

    def part_history(self, pid, limit=300):
        return self.q("SELECT date,kind,qty,ref FROM moves WHERE part_id=? ORDER BY id DESC LIMIT ?", (pid, limit))

    # ── hareket fişleri ──────────────────────────────────────────────
    def create_doc(self, user, doc_type, lines, party="", plate="", note=""):
        """lines: [(part_id, qty, unit_price_kurus)]. Tek transaction; hata olursa hiçbir şey yazılmaz."""
        if doc_type not in DOC_TYPES:
            raise DepoError("Geçersiz fiş tipi.")
        self._require(user, DOC_PERM[doc_type])
        if not lines:
            raise DepoError("Fişe en az bir parça ekleyin.")
        plate = re.sub(r"\s+", " ", plate.strip().upper())
        if plate and not re.fullmatch(r"\d{2} ?[A-Z]{1,3} ?\d{2,5}", plate):
            raise DepoError("Plaka biçimi hatalı (ör. 54 ABC 123).")
        if doc_type == "GIRIS" and not party.strip():
            raise DepoError("Mal kabulde tedarikçi adı zorunludur.")
        merged = {}
        for pid, qty, price in lines:
            qty = parse_int(qty)
            if price < 0:
                raise DepoError("Birim fiyat negatif olamaz.")
            q0, p0 = merged.get(pid, (0, price))
            if p0 != price:
                raise DepoError("Aynı parça farklı fiyatlarla iki kez eklenmiş.")
            merged[pid] = (q0 + qty, price)
        sign, date = DOC_SIGN[doc_type], now_str()
        with self.tx():
            prefix = f"{DOC_PREFIX[doc_type]}-{date[:4]}-"
            last = self.q1("SELECT no FROM docs WHERE no LIKE ? ORDER BY no DESC LIMIT 1", (prefix + "%",))
            no = prefix + f"{(int(last['no'][-5:]) + 1) if last else 1:05d}"
            total = sum(q * p for q, p in merged.values())
            doc_id = self.db.execute("INSERT INTO docs(no,type,date,party,plate,note,total,user_id) "
                                     "VALUES(?,?,?,?,?,?,?,?)",
                                     (no, doc_type, date, party.strip(), plate, note.strip(), total, user.id)).lastrowid
            for pid, (qty, price) in merged.items():
                p = self.q1("SELECT * FROM parts WHERE id=? AND active=1", (pid,))
                if not p:
                    raise DepoError("Fişte geçersiz parça var.")
                if sign < 0 and p["stock"] < qty:
                    raise DepoError(f"Yetersiz stok: {p['brand']} {p['oem']} — {p['name']} "
                                    f"(rafta {p['stock']}, istenen {qty}).")
                if doc_type == "GIRIS":  # ağırlıklı ortalama maliyet
                    new_cost = (p["stock"] * p["avg_cost"] + qty * price + (p["stock"] + qty) // 2) // (p["stock"] + qty)
                    self.db.execute("UPDATE parts SET avg_cost=? WHERE id=?", (new_cost, pid))
                self.db.execute("INSERT INTO doc_lines(doc_id,part_id,qty,unit_price,cost_before) VALUES(?,?,?,?,?)",
                                (doc_id, pid, qty, price, p["avg_cost"]))
                self._move(user, pid, sign * qty, doc_type, no, date)
            self._audit(user, "FIS_" + doc_type, f"{no} {fmt_money(total)}")
        return doc_id

    def cancel_doc(self, user, doc_id, reason):
        self._require(user, "fis.iptal")
        if not reason.strip():
            raise DepoError("İptal nedeni zorunludur.")
        d = self.q1("SELECT * FROM docs WHERE id=?", (doc_id,))
        if not d or d["status"] != "AKTIF":
            raise DepoError("Fiş bulunamadı veya zaten iptal edilmiş.")
        sign = -DOC_SIGN[d["type"]]
        lines = self.q("SELECT l.*, p.stock, p.name FROM doc_lines l JOIN parts p ON p.id=l.part_id WHERE doc_id=?",
                       (doc_id,))
        for ln in lines:
            if ln["stock"] + sign * ln["qty"] < 0:
                raise DepoError(f"İptal edilemez: {ln['name']} stoğu yetersiz kalır.")
        with self.tx():
            for ln in lines:
                self._move(user, ln["part_id"], sign * ln["qty"], "IPTAL", d["no"])
                if d["type"] == "GIRIS":
                    self.db.execute("UPDATE parts SET avg_cost=? WHERE id=?", (ln["cost_before"], ln["part_id"]))
            self.db.execute("UPDATE docs SET status='IPTAL', cancel_reason=? WHERE id=?", (reason.strip(), doc_id))
            self._audit(user, "FIS_IPTAL", f"{d['no']} — {reason.strip()}")

    def list_docs(self, text="", doc_type=None, limit=500):
        like = f"%{text.strip()}%"
        sql = "SELECT * FROM docs WHERE (no LIKE ? OR party LIKE ? OR plate LIKE ?)"
        params = [like, like, like]
        if doc_type:
            sql += " AND type=?"
            params.append(doc_type)
        return self.q(sql + " ORDER BY id DESC LIMIT ?", params + [limit])

    def doc_lines(self, doc_id):
        return self.q("SELECT l.*, p.oem, p.brand, p.name, p.location FROM doc_lines l JOIN parts p "
                      "ON p.id=l.part_id WHERE doc_id=? ORDER BY l.id", (doc_id,))

    # ── depo sayımı ──────────────────────────────────────────────────
    def start_count(self, user, location_prefix=""):
        """Sayım başlatır; kapsamdaki parçaların o anki sistem stoğunu dondurur."""
        self._require(user, "sayim")
        if self.q1("SELECT 1 FROM counts WHERE status='ACIK'"):
            raise DepoError("Açık bir sayım var. Önce onu tamamlayın.")
        scope = location_prefix.strip().upper()
        parts = self.q("SELECT id, stock FROM parts WHERE active=1 AND location LIKE ?", (scope + "%",))
        if not parts:
            raise DepoError("Bu kapsamda sayılacak parça yok.")
        with self.tx():
            cid = self.db.execute("INSERT INTO counts(created,scope,user_id) VALUES(?,?,?)",
                                  (now_str(), scope or "Tüm depo", user.id)).lastrowid
            self.db.executemany("INSERT INTO count_lines(count_id,part_id,system_qty) VALUES(?,?,?)",
                                [(cid, p["id"], p["stock"]) for p in parts])
            self._audit(user, "SAYIM_BASLAT", f"#{cid} {scope or 'Tüm depo'} ({len(parts)} parça)")
        return cid

    def open_count(self):
        return self.q1("SELECT * FROM counts WHERE status='ACIK'")

    def set_counted(self, user, count_id, part_id, counted):
        self._require(user, "sayim")
        counted = None if str(counted).strip() == "" else parse_int(counted, "Sayılan miktar", allow_zero=True)
        with self.tx():
            cur = self.db.execute("UPDATE count_lines SET counted=? WHERE count_id=? AND part_id=? AND "
                                  "count_id IN (SELECT id FROM counts WHERE status='ACIK')", (counted, count_id, part_id))
            if not cur.rowcount:
                raise DepoError("Sayım satırı bulunamadı veya sayım kapanmış.")

    def count_lines(self, count_id):
        return self.q("SELECT c.*, p.oem, p.brand, p.name, p.location, p.avg_cost FROM count_lines c "
                      "JOIN parts p ON p.id=c.part_id WHERE count_id=? ORDER BY p.location, p.name", (count_id,))

    def apply_count(self, user, count_id):
        """Sayılan satırlarda (sayılan - dondurulan sistem stoğu) farkını stoğa uygular.
        Sayım sırasında yapılan giriş/çıkışlar korunur."""
        self._require(user, "sayim")
        c = self.q1("SELECT * FROM counts WHERE id=?", (count_id,))
        if not c or c["status"] != "ACIK":
            raise DepoError("Sayım bulunamadı veya zaten uygulanmış.")
        lines = [ln for ln in self.count_lines(count_id) if ln["counted"] is not None]
        if not lines:
            raise DepoError("Henüz hiçbir parça sayılmadı.")
        changed, value = 0, 0
        with self.tx():
            for ln in lines:
                diff = ln["counted"] - ln["system_qty"]
                if diff:
                    cur = self.q1("SELECT stock FROM parts WHERE id=?", (ln["part_id"],))["stock"]
                    diff = max(diff, -cur)
                    self._move(user, ln["part_id"], diff, "SAYIM", f"Sayım #{count_id}")
                    changed += 1
                    value += diff * ln["avg_cost"]
            self.db.execute("UPDATE counts SET status='UYGULANDI', applied=? WHERE id=?", (now_str(), count_id))
            self._audit(user, "SAYIM_UYGULA", f"#{count_id}: {changed} parçada fark, {fmt_money(value)}")
        return changed, value

    # ── raporlar ─────────────────────────────────────────────────────
    def summary(self):
        r = self.q1("SELECT COUNT(*) n, COALESCE(SUM(stock),0) s, COALESCE(SUM(stock*avg_cost),0) v, "
                    "COALESCE(SUM(stock<=min_stock),0) k FROM parts WHERE active=1")
        today = datetime.now().strftime("%Y-%m-%d")
        out = self.q1("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM docs WHERE type='CIKIS' AND status='AKTIF' "
                      "AND substr(date,1,10)=?", (today,))
        return {"parts": r["n"], "units": r["s"], "value": r["v"], "critical": r["k"],
                "today_out": out["t"], "today_docs": out["n"]}

    def reorder_suggestions(self):
        """Kritik seviyedeki parçalar için öneri: stoğu kritik seviyenin 2 katına tamamla."""
        rows = self.q("SELECT p.*, (SELECT d.party FROM doc_lines l JOIN docs d ON d.id=l.doc_id "
                      "WHERE l.part_id=p.id AND d.type='GIRIS' AND d.status='AKTIF' ORDER BY d.id DESC LIMIT 1) "
                      "AS last_supplier FROM parts p WHERE active=1 AND stock<=min_stock AND min_stock>0 "
                      "ORDER BY last_supplier, p.name")
        return [(r, max(r["min_stock"] * 2 - r["stock"], 1)) for r in rows]

    REPORTS = {
        "deger": "Kategoriye göre stok değeri",
        "siparis": "Sipariş önerisi (kritik stok)",
        "hareketli": "En çok çıkan 20 parça (son 90 gün)",
        "olu": "Ölü stok (90 gündür çıkışı olmayan)",
        "raf": "Raf listesi (sayım için)",
    }

    def report(self, user, key):
        self._require(user, "rapor")
        since = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        if key == "deger":
            rows = self.q("SELECT category, COUNT(*) n, SUM(stock) s, SUM(stock*avg_cost) v FROM parts "
                          "WHERE active=1 GROUP BY category ORDER BY v DESC")
            return (["Kategori", "Parça çeşidi", "Toplam adet", "Stok değeri (maliyet)"],
                    [(r["category"], r["n"], r["s"], fmt_money(r["v"])) for r in rows])
        if key == "siparis":
            return (["Tedarikçi (son alım)", "Marka", "OEM", "Parça", "Stok", "Kritik", "Önerilen sipariş"],
                    [(r["last_supplier"] or "—", r["brand"], r["oem"], r["name"], r["stock"], r["min_stock"], q)
                     for r, q in self.reorder_suggestions()])
        if key == "hareketli":
            rows = self.q("SELECT p.brand,p.oem,p.name,-SUM(m.qty) q FROM moves m JOIN parts p ON p.id=m.part_id "
                          "WHERE m.kind='CIKIS' AND m.date>=? GROUP BY p.id ORDER BY q DESC LIMIT 20", (since,))
            return (["Marka", "OEM", "Parça", "Çıkan adet"], [(r["brand"], r["oem"], r["name"], r["q"]) for r in rows])
        if key == "olu":
            rows = self.q("SELECT p.*, (SELECT MAX(date) FROM moves WHERE part_id=p.id AND kind='CIKIS') last_out "
                          "FROM parts p WHERE active=1 AND stock>0 AND NOT EXISTS (SELECT 1 FROM moves "
                          "WHERE part_id=p.id AND kind='CIKIS' AND date>=?) ORDER BY stock*avg_cost DESC", (since,))
            return (["Marka", "OEM", "Parça", "Raf", "Stok", "Bağlı sermaye", "Son çıkış"],
                    [(r["brand"], r["oem"], r["name"], r["location"], r["stock"], fmt_money(r["stock"] * r["avg_cost"]),
                      (r["last_out"] or "Hiç")[:10]) for r in rows])
        if key == "raf":
            rows = self.q("SELECT * FROM parts WHERE active=1 ORDER BY location='' , location, name")
            return (["Raf", "Kod", "Marka", "OEM", "Parça", "Sistem stoğu", "Sayılan"],
                    [(r["location"] or "—", part_code(r["id"]), r["brand"], r["oem"], r["name"], r["stock"], "")
                     for r in rows])
        raise DepoError("Bilinmeyen rapor.")

    def audit_log(self, limit=500):
        return self.q("SELECT ts,username,action,detail FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ── CSV ve etiket ────────────────────────────────────────────────
    def export_parts_csv(self, path):
        rows = []
        for p in self.search_parts(limit=1_000_000):
            rows.append([p["oem"], p["brand"], p["name"], p["category"], p["location"], p["stock"], p["min_stock"],
                         fmt_money(p["avg_cost"]).replace(" ₺", ""), fmt_money(p["sell_price"]).replace(" ₺", ""),
                         fmt_fitments(self.fitments(p["id"])).replace("\n", "; ")])
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(CSV_HEADERS)
            w.writerows(rows)
        return len(rows)

    def import_parts_csv(self, user, path):
        """Yeni parçaları ekler, var olanları (OEM + marka) günceller. Hatalı satırlar raporlanır, atlanır."""
        self._require(user, "parca.yaz")
        added, updated, errors = 0, 0, []
        with open(path, newline="", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            delim = ";" if sample.count(";") >= sample.count(",") else ","
            reader = csv.DictReader(f, delimiter=delim)
            missing = [h for h in CSV_HEADERS[:3] if h not in (reader.fieldnames or [])]
            if missing:
                raise DepoError("CSV başlıkları eksik: " + ", ".join(missing))
            for n, row in enumerate(reader, start=2):
                g = lambda k, d="": (row.get(k) or d).strip()
                try:
                    cat = g("Kategori", "Diğer") if g("Kategori", "Diğer") in CATEGORIES else "Diğer"
                    fits = g("Uyumlu araçlar").replace("; ", "\n").replace(";", "\n")
                    existing = self.q1("SELECT id, active FROM parts WHERE oem_norm=? AND brand=?",
                                       (norm_oem(g("OEM")), g("Marka").upper()))
                    if existing:
                        self.save_part(user, existing["id"], g("OEM"), g("Marka"), g("Parça adı"), cat, g("Raf"),
                                       g("Kritik", "0"), g("Satış", "0"), fits, active=bool(existing["active"]))
                        updated += 1
                    else:
                        self.save_part(user, None, g("OEM"), g("Marka"), g("Parça adı"), cat, g("Raf"),
                                       g("Kritik", "0"), g("Satış", "0"), fits, g("Stok", "0"), g("Alış", "0"))
                        added += 1
                except DepoError as e:
                    errors.append(f"Satır {n}: {e}")
        return added, updated, errors

    def labels_html(self, user, part_ids, copies=1):
        """Yazdırılabilir raf etiketi sayfası (A4, 3 sütun) üretir."""
        self._require(user, "etiket")
        cards = []
        for pid in part_ids:
            p = self.get_part(pid)
            if not p:
                continue
            code = part_code(pid)
            card = (f'<div class="l"><div class="loc">{html.escape(p["location"] or "—")}</div>'
                    f'<div class="nm">{html.escape(p["name"])}</div>'
                    f'<div class="oem">{html.escape(p["brand"])} · {html.escape(p["oem"])}</div>'
                    f'{code128_svg(code, module=1, height=40)}<div class="cd">{code}</div></div>')
            cards += [card] * max(1, int(copies))
        if not cards:
            raise DepoError("Etiket için parça seçin.")
        return ("<!doctype html><html lang='tr'><head><meta charset='utf-8'><title>Raf etiketleri</title><style>"
                "@page{size:A4;margin:8mm}body{font-family:Arial,sans-serif;margin:0}"
                ".g{display:grid;grid-template-columns:repeat(3,1fr);gap:3mm}"
                ".l{border:1px dashed #999;padding:3mm;height:36mm;box-sizing:border-box;overflow:hidden;"
                "break-inside:avoid;text-align:center}.loc{font-size:18pt;font-weight:bold}"
                ".nm{font-size:9pt;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
                ".oem{font-size:8pt;color:#333;margin-bottom:1mm}.cd{font:9pt monospace}"
                "</style></head><body onload='window.print()'><div class='g'>" + "".join(cards) + "</div></body></html>")

    # ── örnek veri ───────────────────────────────────────────────────
    def seed_demo(self, user):
        if self.q1("SELECT 1 FROM parts LIMIT 1"):
            return
        data = [
            ("7701 208 265", "RENAULT", "Ön fren balatası", "Fren", "A-01-1", 6, "1450", "620",
             "Renault | Clio IV | 2012-2019\nRenault | Captur | 2013-2019\nDacia | Sandero II | 2012-", 14),
            ("0 986 494 596", "BOSCH", "Ön fren balatası", "Fren", "A-01-2", 4, "1180", "540",
             "Renault | Clio IV | 2012-2019\nDacia | Logan II | 2013-", 3),
            ("HU 7008 z", "MANN", "Yağ filtresi", "Filtre", "B-02-1", 10, "420", "190",
             "Volkswagen | Golf VII | 2012-2020\nSkoda | Octavia III | 2013-2020\nSeat | Leon III | 2012-2020", 26),
            ("C 26 168/2", "MANN", "Hava filtresi", "Filtre", "B-02-3", 6, "560", "260",
             "Fiat | Egea | 2015-\nFiat | Doblo | 2010-2022", 4),
            ("K15 KZ 1", "SACHS", "Ön amortisör (sağ)", "Süspansiyon", "C-04-1", 2, "3650", "2100",
             "Fiat | Egea | 2015-", 2),
            ("7700 500 168", "RENAULT", "Triger seti", "Motor", "C-01-2", 2, "4850", "3100",
             "Renault | Megane III | 2008-2016\nRenault | Fluence | 2010-2016", 5),
            ("0 280 158 040", "BOSCH", "Enjektör", "Motor", "C-02-1", 1, "5200", "3600",
             "Volkswagen | Golf VII | 2012-2020", 0),
            ("10W-40 4L", "CASTROL", "Motor yağı 10W-40 4 L", "Yağ ve sıvılar", "D-01-1", 12, "1350", "880", "", 40),
        ]
        ids = []
        for oem, brand, name, cat, loc, mn, sell, cost, fits, stock in data:
            ids.append(self.save_part(user, None, oem, brand, name, cat, loc, mn, sell, fits, stock, cost))
        self.create_doc(user, "GIRIS", [(ids[2], 10, 18500), (ids[7], 12, 86000)], party="Sakarya Oto Yedek Parça")
        self.create_doc(user, "CIKIS", [(ids[0], 4, 145000), (ids[2], 1, 42000)], party="Servis", plate="54 ABC 123")
        self.create_doc(user, "CIKIS", [(ids[7], 6, 135000)], party="Tezgâh satışı")


# ════════════════════════════════════════════════════════════════════
#  Birim testleri
# ════════════════════════════════════════════════════════════════════
class DepoTests(unittest.TestCase):
    def setUp(self):
        self.d = Depo(":memory:")
        self.a = self.d.login("admin", "admin123")
        self.pid = self.d.save_part(self.a, None, "7701-208 265", "renault", "Fren balatası", "Fren", "a 1 2",
                                    "3", "1450", "Renault | Clio IV | 2012-2019\nDacia | Sandero | 2012-", 10, "600")

    def test_helpers(self):
        self.assertEqual(norm_oem(" 77-01 208.265 "), "7701208265")
        self.assertEqual(norm_location("a 3 2"), "A-03-2")
        self.assertRaises(DepoError, norm_location, "rafın üstü")
        self.assertEqual(parse_fitments("ford | focus | 2011-2018\nfiat|egea|2015-")[1], ("Fiat", "egea", 2015, 2100))
        self.assertRaises(DepoError, parse_fitments, "sadece marka")
        self.assertRaises(DepoError, parse_fitments, "A | B | 2019-2012")
        self.assertEqual(fmt_money(123456), "1.234,56 ₺")

    def test_search_variants(self):
        p = self.d.get_part(self.pid)
        self.assertEqual(p["location"], "A-01-2")
        self.assertEqual(p["brand"], "RENAULT")
        for q in ("7701208265", "7701 208 265", "77-01-208", "balata", part_code(self.pid), "A-01", "clio",
                  "renault clio"):
            self.assertEqual(len(self.d.search_parts(q)), 1, q)
        self.assertEqual(len(self.d.search_parts(make="dacia", year=2020)), 1)
        self.assertEqual(len(self.d.search_parts(make="renault", year=2021)), 0)
        self.assertEqual(len(self.d.search_parts(model="Clio", year=2015)), 1)
        self.assertEqual(self.d.search_parts("' OR 1=1 --"), [])

    def test_duplicate_oem_same_brand(self):
        self.assertRaises(DepoError, self.d.save_part, self.a, None, "7701208265", "RENAULT", "Kopya", "Fren", "",
                          "0", "1")
        self.d.save_part(self.a, None, "7701208265", "BOSCH", "Muadil", "Fren", "", "0", "1")

    def test_receipt_weighted_cost(self):
        self.d.create_doc(self.a, "GIRIS", [(self.pid, 10, 80000)], party="Tedarikçi")
        p = self.d.get_part(self.pid)
        self.assertEqual(p["stock"], 20)
        self.assertEqual(p["avg_cost"], 70000)  # (10*600 + 10*800) / 20

    def test_issue_and_stock_guard(self):
        self.d.create_doc(self.a, "CIKIS", [(self.pid, 4, 145000)], plate="54abc123")
        self.assertEqual(self.d.get_part(self.pid)["stock"], 6)
        with self.assertRaisesRegex(DepoError, "Yetersiz stok"):
            self.d.create_doc(self.a, "CIKIS", [(self.pid, 7, 145000)])
        self.assertEqual(self.d.get_part(self.pid)["stock"], 6)
        self.assertEqual(len(self.d.list_docs()), 1)
        self.assertRaises(DepoError, self.d.create_doc, self.a, "CIKIS", [(self.pid, 1, 1)], plate="ABC")
        self.assertRaises(DepoError, self.d.create_doc, self.a, "GIRIS", [(self.pid, 1, 1)], party="")

    def test_doc_numbering_and_cancel(self):
        a = self.d.create_doc(self.a, "GIRIS", [(self.pid, 10, 80000)], party="X")
        b = self.d.create_doc(self.a, "GIRIS", [(self.pid, 1, 80000)], party="X")
        no = [self.d.q1("SELECT no FROM docs WHERE id=?", (i,))["no"] for i in (a, b)]
        self.assertTrue(no[0].startswith("GF-") and int(no[1][-5:]) == int(no[0][-5:]) + 1)
        self.d.cancel_doc(self.a, b, "Yanlış miktar")
        self.d.cancel_doc(self.a, a, "Yanlış tedarikçi")
        p = self.d.get_part(self.pid)
        self.assertEqual((p["stock"], p["avg_cost"]), (10, 60000))
        self.assertRaises(DepoError, self.d.cancel_doc, self.a, a, "tekrar")

    def test_return(self):
        c = self.d.create_doc(self.a, "CIKIS", [(self.pid, 2, 145000)])
        self.d.create_doc(self.a, "IADE", [(self.pid, 1, 145000)], note=f"Fiş #{c}")
        self.assertEqual(self.d.get_part(self.pid)["stock"], 9)

    def test_roles(self):
        self.d.add_user(self.a, "tezgah1", "Tezgah123", "tezgah")
        self.d.add_user(self.a, "depo1", "Depocu123", "depocu")
        t, dp = self.d.login("tezgah1", "Tezgah123"), self.d.login("depo1", "Depocu123")
        self.assertRaises(DepoError, self.d.create_doc, t, "GIRIS", [(self.pid, 1, 1)], party="X")
        self.assertRaises(DepoError, self.d.save_part, t, None, "1", "X", "X", "Diğer", "", "0", "0")
        self.assertRaises(DepoError, self.d.create_doc, dp, "CIKIS", [(self.pid, 1, 1)])
        self.assertRaises(DepoError, self.d.report, dp, "deger")
        self.d.create_doc(t, "CIKIS", [(self.pid, 1, 1)])
        self.d.create_doc(dp, "GIRIS", [(self.pid, 1, 1)], party="X")
        self.assertRaises(DepoError, self.d.cancel_doc, dp, 1, "x")

    def test_login_lockout_and_password(self):
        for _ in range(MAX_FAILED_LOGINS):
            self.assertRaises(DepoError, self.d.login, "admin", "yanlis")
        self.assertRaisesRegex(DepoError, "kilitli", self.d.login, "admin", "admin123")
        self.assertRaises(DepoError, self.d.change_password, self.a, "admin123", "kisa")

    def test_count_keeps_moves_during_count(self):
        cid = self.d.start_count(self.a, "A")
        self.assertRaises(DepoError, self.d.start_count, self.a)
        self.d.create_doc(self.a, "CIKIS", [(self.pid, 2, 1)])   # sayım sırasında satış
        self.d.set_counted(self.a, cid, self.pid, "9")           # rafta 1 eksik bulundu
        changed, _ = self.d.apply_count(self.a, cid)
        self.assertEqual(changed, 1)
        self.assertEqual(self.d.get_part(self.pid)["stock"], 7)   # 10 - 2 satış - 1 sayım farkı
        self.assertRaises(DepoError, self.d.apply_count, self.a, cid)
        self.assertRaises(DepoError, self.d.set_counted, self.a, cid, self.pid, "3")

    def test_reorder_and_reports(self):
        self.d.create_doc(self.a, "CIKIS", [(self.pid, 8, 1)])
        sugg = self.d.reorder_suggestions()
        self.assertEqual(sugg[0][1], 4)  # kritik 3 -> hedef 6, stok 2
        for key in Depo.REPORTS:
            self.assertTrue(self.d.report(self.a, key)[0])

    def test_csv_roundtrip(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "parcalar.csv")
        self.assertEqual(self.d.export_parts_csv(path), 1)
        with open(path, "a", encoding="utf-8") as f:
            f.write("HU 7008 z;mann;Yağ filtresi;Filtre;B-2-1;5;2;190;420;Volkswagen | Golf VII | 2012-2020\n")
            f.write(";;eksik satır;;;;;;;\n")
        added, updated, errors = self.d.import_parts_csv(self.a, path)
        self.assertEqual((added, updated, len(errors)), (1, 1, 1))
        self.assertEqual(len(self.d.search_parts(make="volkswagen")), 1)
        self.assertEqual(self.d.get_part(self.pid)["stock"], 10)  # güncellemede stok ezilmez

    def test_code128(self):
        widths = code128_widths("P000001")
        self.assertEqual(sum(widths), 11 * (len("P000001") + 3) + 2)
        self.assertTrue(all(sum(map(int, p)) == 11 for p in CODE128[:106]))
        self.assertIn("<svg", self.d.labels_html(self.a, [self.pid], copies=2))
        self.assertRaises(DepoError, code128_widths, "ç")

    def test_demo(self):
        d = Depo(":memory:")
        a = d.login("admin", "admin123")
        d.seed_demo(a)
        self.assertEqual(d.summary()["parts"], 8)
        self.assertGreater(len(d.reorder_suggestions()), 0)


# ════════════════════════════════════════════════════════════════════
#  Arayüz (tkinter / ttk)
# ════════════════════════════════════════════════════════════════════
def run_gui(depo, demo=False):
    import tempfile
    import tkinter as tk
    import webbrowser
    from tkinter import ttk, messagebox, filedialog

    C = dict(bg="#eef1f5", panel="#1f2a37", panel_fg="#cbd5e1", card="#ffffff", fg="#1f2937", muted="#6b7280",
             accent="#f97316", accent2="#ea580c", ok="#15803d", warn="#b45309", bad="#dc2626", line="#d6dbe2",
             sel="#fde6d3")
    FONT = ("Segoe UI", 10)

    def setup_style(root):
        st = ttk.Style(root)
        st.theme_use("clam")
        root.configure(bg=C["bg"])
        root.option_add("*TCombobox*Listbox.font", FONT)
        st.configure(".", background=C["bg"], foreground=C["fg"], font=FONT, bordercolor=C["line"],
                     lightcolor=C["line"], darkcolor=C["line"], fieldbackground=C["card"])
        st.configure("TFrame", background=C["bg"])
        st.configure("Card.TFrame", background=C["card"])
        st.configure("Side.TFrame", background=C["panel"])
        st.configure("TLabel", background=C["bg"], foreground=C["fg"])
        st.configure("Muted.TLabel", foreground=C["muted"])
        st.configure("Card.TLabel", background=C["card"], foreground=C["muted"])
        st.configure("CardBig.TLabel", background=C["card"], foreground=C["fg"], font=("Segoe UI", 16, "bold"))
        st.configure("Loc.TLabel", background=C["card"], foreground=C["accent2"], font=("Segoe UI", 26, "bold"))
        st.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("Side.TLabel", background=C["panel"], foreground=C["panel_fg"])
        st.configure("Logo.TLabel", background=C["panel"], foreground="white", font=("Segoe UI", 15, "bold"))
        st.configure("TButton", background=C["card"], foreground=C["fg"], padding=(12, 6))
        st.map("TButton", background=[("active", C["line"])])
        st.configure("Accent.TButton", background=C["accent"], foreground="white", bordercolor=C["accent"])
        st.map("Accent.TButton", background=[("active", C["accent2"])])
        st.configure("Nav.TButton", background=C["panel"], foreground=C["panel_fg"], anchor="w", padding=(18, 10),
                     bordercolor=C["panel"], lightcolor=C["panel"], darkcolor=C["panel"])
        st.map("Nav.TButton", background=[("active", "#2b3a4c")], foreground=[("active", "white")])
        st.configure("NavActive.TButton", background="#2b3a4c", foreground="white", anchor="w", padding=(18, 10),
                     bordercolor=C["accent"], lightcolor="#2b3a4c", darkcolor="#2b3a4c")
        st.configure("TEntry", padding=5)
        st.configure("Big.TEntry", padding=9)
        st.configure("TCombobox", padding=4)
        st.configure("Treeview", background=C["card"], fieldbackground=C["card"], rowheight=28)
        st.map("Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["fg"])])
        st.configure("Treeview.Heading", background="#e5e9ef", foreground=C["muted"], relief="flat", padding=6)

    def make_tree(parent, cols, height=15, select="browse"):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", height=height, selectmode=select)
        for key, title, width, anchor in cols:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor, stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag in ("ok", "warn", "bad", "muted"):
            tree.tag_configure(tag, foreground=C[tag] if tag != "muted" else C["muted"])
        return frame, tree

    def fill_tree(tree, rows, tags=None):
        tree.delete(*tree.get_children())
        for i, r in enumerate(rows):
            tree.insert("", "end", iid=str(r[0]), values=r[1:], tags=(tags[i],) if tags and tags[i] else ())

    def selected_id(tree, what="kayıt"):
        sel = tree.selection()
        if not sel:
            raise DepoError(f"Önce bir {what} seçin.")
        return int(sel[0])

    def stock_tag(p):
        return "bad" if p["stock"] == 0 else "warn" if p["stock"] <= p["min_stock"] else ""

    class FormDialog(tk.Toplevel):
        """fields: dict(key,label,kind=entry|password|combo|check|text, values, default)"""
        def __init__(self, master, title, fields, on_submit, note=""):
            super().__init__(master)
            self.title(title)
            self.configure(bg=C["bg"])
            self.transient(master.winfo_toplevel())
            self.resizable(False, False)
            self.on_submit, self.getters = on_submit, {}
            body = ttk.Frame(self, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=title, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                   pady=(0, 12))
            first = None
            for i, f in enumerate(fields, start=1):
                kind = f.get("kind", "entry")
                if kind == "check":
                    v = tk.BooleanVar(value=f.get("default", True))
                    ttk.Checkbutton(body, text=f["label"], variable=v).grid(row=i, column=1, sticky="w", pady=4)
                    self.getters[f["key"]] = v.get
                    continue
                ttk.Label(body, text=f["label"], style="Muted.TLabel").grid(row=i, column=0, sticky="nw",
                                                                            padx=(0, 12), pady=6)
                if kind == "text":
                    w = tk.Text(body, width=46, height=5, font=FONT, relief="solid", bd=1, highlightthickness=0)
                    w.insert("1.0", f.get("default", ""))
                    self.getters[f["key"]] = lambda w=w: w.get("1.0", "end").strip()
                else:
                    v = tk.StringVar(value=str(f.get("default", "")))
                    if kind == "combo":
                        w = ttk.Combobox(body, textvariable=v, values=f["values"], state="readonly", width=44)
                    else:
                        w = ttk.Entry(body, textvariable=v, width=46, show="•" if kind == "password" else "")
                    self.getters[f["key"]] = v.get
                w.grid(row=i, column=1, sticky="ew", pady=4)
                first = first or w
            if note:
                ttk.Label(body, text=note, style="Muted.TLabel", wraplength=420).grid(
                    row=len(fields) + 1, column=1, sticky="w", pady=(4, 0))
            btns = ttk.Frame(body)
            btns.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", pady=(14, 0))
            ttk.Button(btns, text="Vazgeç", command=self.destroy).pack(side="right", padx=(8, 0))
            ttk.Button(btns, text="Kaydet", style="Accent.TButton", command=self.submit).pack(side="right")
            self.bind("<Escape>", lambda e: self.destroy())
            if first:
                first.focus_set()
            self.update_idletasks()
            top = master.winfo_toplevel()
            self.geometry(f"+{top.winfo_rootx() + 160}+{top.winfo_rooty() + 60}")
            self.grab_set()

        def submit(self):
            try:
                self.on_submit({k: g() for k, g in self.getters.items()})
            except DepoError as e:
                messagebox.showerror("Hata", str(e), parent=self)
                return
            self.destroy()

    class Page(ttk.Frame):
        title = ""

        def __init__(self, app):
            super().__init__(app.content, padding=20)
            self.app, self.depo, self.user = app, app.depo, app.user
            head = ttk.Frame(self)
            head.pack(fill="x", pady=(0, 14))
            ttk.Label(head, text=self.title, style="Title.TLabel").pack(side="left")
            self.toolbar = ttk.Frame(head)
            self.toolbar.pack(side="right")
            self.build()

        def build(self):
            pass

        def refresh(self):
            pass

        def btn(self, text, cmd, accent=False, perm=None):
            if perm and not self.depo.can(self.user, perm):
                return
            ttk.Button(self.toolbar, text=text, command=self.guard(cmd),
                       style="Accent.TButton" if accent else "TButton").pack(side="left", padx=(8, 0))

        def guard(self, fn):
            def wrapped(*a):
                try:
                    fn(*a)
                except DepoError as e:
                    messagebox.showerror("Hata", str(e), parent=self)
            return wrapped

    # ── Parça arama (ana ekran) ──────────────────────────────────────
    class PartsPage(Page):
        title = "Parça Arama"

        def build(self):
            self.btn("Yeni parça", self.new, accent=True, perm="parca.yaz")
            self.btn("Düzenle", self.edit, perm="parca.yaz")
            self.btn("Hareketler", self.history)
            self.btn("Raf etiketi", self.labels, perm="etiket")
            self.btn("CSV içe aktar", self.import_csv, perm="parca.yaz")
            self.btn("CSV dışa aktar", self.export_csv)

            bar = ttk.Frame(self, style="Card.TFrame", padding=12)
            bar.pack(fill="x", pady=(0, 12))
            self.q = tk.StringVar()
            self.make, self.model, self.year, self.cat = (tk.StringVar() for _ in range(4))
            ttk.Label(bar, text="OEM no, parça adı, marka, raf veya barkod", style="Card.TLabel").grid(
                row=0, column=0, sticky="w")
            e = ttk.Entry(bar, textvariable=self.q, width=40, style="Big.TEntry", font=("Segoe UI", 12))
            e.grid(row=1, column=0, padx=(0, 14), sticky="ew")
            e.focus_set()
            e.bind("<Return>", lambda ev: self.select_first())
            for col, (label, var, width) in enumerate(
                    [("Araç markası", self.make, 14), ("Model", self.model, 14), ("Yıl", self.year, 7)], start=1):
                ttk.Label(bar, text=label, style="Card.TLabel").grid(row=0, column=col, sticky="w")
                w = ttk.Combobox(bar, textvariable=var, values=self.depo.makes(), width=width) if var is self.make \
                    else ttk.Entry(bar, textvariable=var, width=width)
                w.grid(row=1, column=col, padx=(0, 10))
            ttk.Label(bar, text="Kategori", style="Card.TLabel").grid(row=0, column=4, sticky="w")
            ttk.Combobox(bar, textvariable=self.cat, values=[""] + CATEGORIES, state="readonly", width=16).grid(
                row=1, column=4)
            bar.columnconfigure(0, weight=1)
            for v in (self.q, self.make, self.model, self.year, self.cat):
                v.trace_add("write", lambda *_: self.refresh())

            main = ttk.Frame(self)
            main.pack(fill="both", expand=True)
            f, self.tree = make_tree(main, [("loc", "Raf", 80, "center"), ("brand", "Marka", 90, "w"),
                                            ("oem", "OEM no", 120, "w"), ("name", "Parça adı", 160, "w"),
                                            ("cat", "Kategori", 90, "w"), ("stock", "Stok", 55, "center"),
                                            ("price", "Satış", 100, "e")], select="extended")
            self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
            self.tree.bind("<Double-1>", lambda e: self.guard(self.edit)() if self.depo.can(self.user, "parca.yaz")
                           else None)

            self.detail = ttk.Frame(main, style="Card.TFrame", padding=16, width=300)
            self.detail.pack(side="right", fill="y", padx=(12, 0))
            self.detail.pack_propagate(False)
            f.pack(side="left", fill="both", expand=True)
            self.d_loc = ttk.Label(self.detail, text="—", style="Loc.TLabel")
            self.d_loc.pack(anchor="w")
            ttk.Label(self.detail, text="Raf adresi", style="Card.TLabel").pack(anchor="w")
            self.d_info = ttk.Label(self.detail, text="Bir parça seçin.", style="Card.TLabel", wraplength=262,
                                    justify="left", foreground=C["fg"])
            self.d_info.pack(anchor="w", pady=(14, 8))
            ttk.Label(self.detail, text="Uyumlu araçlar", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(
                anchor="w")
            self.d_fit = ttk.Label(self.detail, text="", style="Card.TLabel", wraplength=262, justify="left",
                                   foreground=C["fg"])
            self.d_fit.pack(anchor="w", pady=(4, 0))

        def refresh(self):
            year = self.year.get().strip()
            if year and not (year.isdigit() and len(year) == 4):
                return
            rows = self.depo.search_parts(self.q.get(), self.make.get(), self.model.get(), year or None,
                                          self.cat.get())
            fill_tree(self.tree, [(r["id"], r["location"] or "—", r["brand"], r["oem"], r["name"], r["category"],
                                   r["stock"], fmt_money(r["sell_price"])) for r in rows], [stock_tag(r) for r in rows])
            self.show_detail()

        def select_first(self):
            kids = self.tree.get_children()
            if kids:
                self.tree.selection_set(kids[0])
                self.tree.focus(kids[0])

        def show_detail(self):
            sel = self.tree.selection()
            if not sel:
                self.d_loc.configure(text="—")
                self.d_info.configure(text="Bir parça seçin.")
                self.d_fit.configure(text="")
                return
            p = self.depo.get_part(int(sel[0]))
            self.d_loc.configure(text=p["location"] or "Rafsız")
            self.d_info.configure(text=f"{p['name']}\n{p['brand']} · {p['oem']}\nKod: {part_code(p['id'])}\n\n"
                                       f"Stok: {p['stock']}  (kritik {p['min_stock']})\n"
                                       f"Satış: {fmt_money(p['sell_price'])}\n"
                                       f"Ort. maliyet: {fmt_money(p['avg_cost'])}")
            self.d_fit.configure(text=fmt_fitments(self.depo.fitments(p["id"])) or "Tanımlı değil")

        def _form(self, p=None):
            g = lambda k, d="": p[k] if p else d
            money = lambda k: fmt_money(p[k]).replace(" ₺", "") if p else ""
            fields = [dict(key="oem", label="OEM numarası", default=g("oem")),
                      dict(key="brand", label="Parça markası", default=g("brand")),
                      dict(key="name", label="Parça adı", default=g("name")),
                      dict(key="cat", label="Kategori", kind="combo", values=CATEGORIES, default=g("category", "Diğer")),
                      dict(key="loc", label="Raf adresi", default=g("location")),
                      dict(key="min", label="Kritik stok", default=g("min_stock", "0")),
                      dict(key="sell", label="Satış fiyatı (₺)", default=money("sell_price")),
                      dict(key="fit", label="Uyumlu araçlar", kind="text",
                           default=fmt_fitments(self.depo.fitments(p["id"])) if p else "")]
            if p:
                fields.append(dict(key="active", label="Aktif", kind="check", default=bool(p["active"])))
            else:
                fields += [dict(key="open", label="Açılış stoğu", default="0"),
                           dict(key="cost", label="Birim maliyet (₺)", default="0")]

            def save(v):
                self.depo.save_part(self.user, p["id"] if p else None, v["oem"], v["brand"], v["name"], v["cat"],
                                    v["loc"], v["min"], v["sell"], v["fit"],
                                    v.get("open", 0), v.get("cost", "0"), v.get("active", True))
                self.refresh()
            FormDialog(self, "Parça düzenle" if p else "Yeni parça", fields, save,
                       note="Uyumlu araçlar: her satıra 'Marka | Model | 2012-2019'. Raf adresi: A-03-2.")

        def new(self):
            self._form()

        def edit(self):
            self._form(self.depo.get_part(selected_id(self.tree, "parça")))

        def history(self):
            p = self.depo.get_part(selected_id(self.tree, "parça"))
            win = tk.Toplevel(self, bg=C["bg"])
            win.title(f"Hareketler — {p['brand']} {p['oem']}")
            win.geometry("640x420")
            names = {"ACILIS": "Açılış", "GIRIS": "Mal kabul", "CIKIS": "Çıkış", "IADE": "İade", "IPTAL": "Fiş iptali",
                     "SAYIM": "Sayım farkı"}
            f, t = make_tree(win, [("d", "Tarih", 150, "w"), ("k", "Hareket", 120, "w"), ("q", "Miktar", 80, "center"),
                                   ("r", "Belge", 200, "w")])
            f.pack(fill="both", expand=True, padx=14, pady=14)
            rows = self.depo.part_history(p["id"])
            fill_tree(t, [(i, r["date"], names.get(r["kind"], r["kind"]), f"{r['qty']:+d}", r["ref"] or "")
                          for i, r in enumerate(rows)], ["ok" if r["qty"] > 0 else "bad" for r in rows])

        def labels(self):
            ids = [int(i) for i in self.tree.selection()]

            def save(v):
                page = self.depo.labels_html(self.user, ids, parse_int(v["n"], "Kopya sayısı"))
                path = os.path.join(tempfile.gettempdir(), f"raf_etiketleri_{datetime.now():%H%M%S}.html")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(page)
                webbrowser.open("file://" + path)
            if not ids:
                raise DepoError("Etiket basılacak parçaları seçin (Ctrl ile çoklu seçim).")
            FormDialog(self, f"Raf etiketi ({len(ids)} parça)", [dict(key="n", label="Kopya sayısı", default="1")],
                       save, note="Etiket sayfası tarayıcıda açılır ve yazdırma penceresi gelir.")

        def import_csv(self):
            path = filedialog.askopenfilename(parent=self, filetypes=[("CSV", "*.csv")])
            if not path:
                return
            added, updated, errors = self.depo.import_parts_csv(self.user, path)
            msg = f"{added} parça eklendi, {updated} parça güncellendi."
            if errors:
                msg += f"\n\n{len(errors)} satır atlandı:\n" + "\n".join(errors[:12])
            messagebox.showinfo("CSV içe aktarma", msg, parent=self)
            self.refresh()

        def export_csv(self):
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile="parcalar.csv")
            if path:
                n = self.depo.export_parts_csv(path)
                messagebox.showinfo("CSV", f"{n} parça dışa aktarıldı.", parent=self)

    # ── Fiş oluşturma ────────────────────────────────────────────────
    class DocDialog(tk.Toplevel):
        def __init__(self, page, doc_type):
            super().__init__(page)
            self.page, self.depo, self.user, self.type = page, page.depo, page.user, doc_type
            self.lines = []
            self.title(DOC_TYPES[doc_type])
            self.configure(bg=C["bg"])
            self.geometry("1020x640")
            self.minsize(960, 560)
            self.transient(page.winfo_toplevel())
            body = ttk.Frame(self, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=DOC_TYPES[doc_type], style="Title.TLabel").pack(anchor="w")

            top = ttk.Frame(body)
            top.pack(fill="x", pady=(10, 8))
            self.party, self.plate, self.note = tk.StringVar(), tk.StringVar(), tk.StringVar()
            party_label = {"GIRIS": "Tedarikçi *", "CIKIS": "Müşteri / servis", "IADE": "İade eden"}[doc_type]
            for label, var, w in ((party_label, self.party, 30), ("Araç plakası", self.plate, 14),
                                  ("Not", self.note, 30)):
                if doc_type == "GIRIS" and var is self.plate:
                    continue
                ttk.Label(top, text=label, style="Muted.TLabel").pack(side="left")
                ttk.Entry(top, textvariable=var, width=w).pack(side="left", padx=(6, 18))

            add = ttk.Frame(body, style="Card.TFrame", padding=10)
            add.pack(fill="x", pady=(4, 8))
            ttk.Label(add, text="Parça ara (OEM, ad veya barkod okut)", style="Card.TLabel").grid(row=0, column=0,
                                                                                               sticky="w")
            self.search = tk.StringVar()
            se = ttk.Entry(add, textvariable=self.search, width=26)
            se.grid(row=1, column=0, padx=(0, 8), sticky="w")
            se.focus_set()
            se.bind("<Return>", lambda e: page.guard(self.quick_add)())
            self.search.trace_add("write", lambda *_: self.update_matches())
            ttk.Label(add, text="Eşleşen parça", style="Card.TLabel").grid(row=0, column=1, sticky="w")
            self.match = ttk.Combobox(add, state="readonly", width=40)
            self.match.grid(row=1, column=1, padx=(0, 8))
            self.match.bind("<<ComboboxSelected>>", lambda e: self.fill_price())
            ttk.Label(add, text="Miktar", style="Card.TLabel").grid(row=0, column=2, sticky="w")
            self.qty = ttk.Entry(add, width=7)
            self.qty.insert(0, "1")
            self.qty.grid(row=1, column=2, padx=(0, 8))
            ttk.Label(add, text="Birim fiyat (₺)", style="Card.TLabel").grid(row=0, column=3, sticky="w")
            self.price = ttk.Entry(add, width=11)
            self.price.grid(row=1, column=3, padx=(0, 8))
            ttk.Button(add, text="Ekle", style="Accent.TButton", command=page.guard(self.add_line)).grid(row=1,
                                                                                                         column=4)
            self.match_map = {}

            f, self.tree = make_tree(body, [("loc", "Raf", 70, "center"), ("brand", "Marka", 90, "w"),
                                            ("oem", "OEM", 130, "w"), ("name", "Parça", 230, "w"),
                                            ("qty", "Miktar", 70, "center"), ("price", "Birim fiyat", 110, "e"),
                                            ("total", "Tutar", 120, "e")], 9)
            f.pack(fill="both", expand=True)
            self.total = ttk.Label(body, text="", font=("Segoe UI", 12, "bold"))
            self.total.pack(anchor="e", pady=(8, 0))
            bottom = ttk.Frame(body)
            bottom.pack(fill="x", pady=(8, 0))
            ttk.Button(bottom, text="Seçili satırı sil", command=self.remove_line).pack(side="left")
            ttk.Button(bottom, text="Vazgeç", command=self.destroy).pack(side="right", padx=(8, 0))
            ttk.Button(bottom, text="Fişi kaydet", style="Accent.TButton",
                       command=page.guard(self.save)).pack(side="right")
            self.update_matches()
            self.redraw()
            self.grab_set()

        def update_matches(self):
            text = self.search.get()
            rows = self.depo.search_parts(text, limit=40) if text.strip() else []
            self.match_map = {f"[{r['location'] or '—'}] {r['brand']} {r['oem']} — {r['name']} (stok {r['stock']})": r
                              for r in rows}
            self.match["values"] = list(self.match_map)
            if rows:
                self.match.current(0)
            else:
                self.match.set("")
            self.fill_price()

        def fill_price(self):
            p = self.match_map.get(self.match.get())
            self.price.delete(0, "end")
            if p:
                self.price.insert(0, fmt_money(p["avg_cost"] if self.type == "GIRIS" else p["sell_price"])
                                  .replace(" ₺", ""))

        def quick_add(self):
            """Barkod okuyucu kodu yazıp Enter'a basınca tek eşleşmeyi doğrudan ekler."""
            if len(self.match_map) == 1:
                self.add_line()

        def add_line(self):
            p = self.match_map.get(self.match.get())
            if not p:
                raise DepoError("Önce parçayı arayıp seçin.")
            qty, price = parse_int(self.qty.get()), parse_money(self.price.get())
            for i, (lp, lq, lpr) in enumerate(self.lines):
                if lp["id"] == p["id"] and lpr == price:
                    self.lines[i] = (lp, lq + qty, lpr)
                    break
            else:
                self.lines.append((p, qty, price))
            self.search.set("")
            self.qty.delete(0, "end")
            self.qty.insert(0, "1")
            self.redraw()

        def remove_line(self):
            sel = self.tree.selection()
            if sel:
                del self.lines[int(sel[0])]
                self.redraw()

        def redraw(self):
            fill_tree(self.tree, [(i, p["location"] or "—", p["brand"], p["oem"], p["name"], q, fmt_money(pr),
                                   fmt_money(q * pr)) for i, (p, q, pr) in enumerate(self.lines)])
            self.total.configure(text=f"Toplam: {fmt_money(sum(q * pr for _, q, pr in self.lines))}"
                                      f"   ·   {sum(q for _, q, _ in self.lines)} adet")

        def save(self):
            doc_id = self.depo.create_doc(self.user, self.type, [(p["id"], q, pr) for p, q, pr in self.lines],
                                          self.party.get(), self.plate.get(), self.note.get())
            no = self.depo.q1("SELECT no FROM docs WHERE id=?", (doc_id,))["no"]
            messagebox.showinfo("Kaydedildi", f"{no} numaralı fiş kaydedildi.", parent=self)
            self.destroy()
            self.page.refresh()

    class DocsPage(Page):
        title = "Hareket Fişleri"

        def build(self):
            self.btn("Satış / servis çıkışı", lambda: DocDialog(self, "CIKIS"), accent=True, perm="cikis")
            self.btn("Mal kabul", lambda: DocDialog(self, "GIRIS"), accent=not self.depo.can(self.user, "cikis"),
                     perm="giris")
            self.btn("İade", lambda: DocDialog(self, "IADE"), perm="iade")
            self.btn("Detay", self.detail)
            self.btn("İptal et", self.cancel, perm="fis.iptal")
            self.q = tk.StringVar()
            self.q.trace_add("write", lambda *_: self.refresh())
            ttk.Entry(self.toolbar, textvariable=self.q, width=22).pack(side="left", padx=(8, 0))
            f, self.tree = make_tree(self, [("no", "Fiş no", 120, "w"), ("type", "Tip", 150, "w"),
                                            ("date", "Tarih", 140, "w"), ("party", "Cari / açıklama", 200, "w"),
                                            ("plate", "Plaka", 100, "center"), ("total", "Tutar", 120, "e"),
                                            ("st", "Durum", 70, "center")])
            f.pack(fill="both", expand=True)
            self.tree.bind("<Double-1>", lambda e: self.guard(self.detail)())

        def refresh(self):
            rows = self.depo.list_docs(self.q.get())
            fill_tree(self.tree, [(r["id"], r["no"], DOC_TYPES[r["type"]], r["date"][:16], r["party"] or "",
                                   r["plate"] or "", fmt_money(r["total"]), "Aktif" if r["status"] == "AKTIF" else "İptal")
                                  for r in rows],
                      ["muted" if r["status"] == "IPTAL" else "ok" if r["type"] == "GIRIS" else "" for r in rows])

        def detail(self):
            did = selected_id(self.tree, "fiş")
            d = self.depo.q1("SELECT * FROM docs WHERE id=?", (did,))
            win = tk.Toplevel(self, bg=C["bg"])
            win.title(f"Fiş {d['no']}")
            win.geometry("760x420")
            info = f"{d['no']} · {DOC_TYPES[d['type']]} · {d['date'][:16]} · {d['party'] or ''} {d['plate'] or ''}"
            if d["status"] == "IPTAL":
                info += f" · İPTAL ({d['cancel_reason']})"
            ttk.Label(win, text=info, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 6))
            f, t = make_tree(win, [("l", "Raf", 70, "center"), ("b", "Marka", 90, "w"), ("o", "OEM", 130, "w"),
                                   ("n", "Parça", 220, "w"), ("q", "Miktar", 60, "center"), ("p", "Tutar", 110, "e")], 8)
            f.pack(fill="both", expand=True, padx=14)
            fill_tree(t, [(r["id"], r["location"] or "—", r["brand"], r["oem"], r["name"], r["qty"],
                           fmt_money(r["qty"] * r["unit_price"])) for r in self.depo.doc_lines(did)])
            ttk.Label(win, text=f"Toplam {fmt_money(d['total'])}", font=("Segoe UI", 11, "bold")).pack(
                anchor="e", padx=14, pady=14)

        def cancel(self):
            did = selected_id(self.tree, "fiş")

            def save(v):
                self.depo.cancel_doc(self.user, did, v["r"])
                self.refresh()
            FormDialog(self, "Fiş iptali", [dict(key="r", label="İptal nedeni")], save,
                       note="İptal, stok hareketlerini ters kayıtla geri alır; fiş silinmez.")

    # ── Sayım ────────────────────────────────────────────────────────
    class CountPage(Page):
        title = "Depo Sayımı"

        def build(self):
            self.btn("Yeni sayım başlat", self.start, accent=True)
            self.btn("Seçili satıra miktar gir", self.enter)
            self.btn("Farkları stoğa uygula", self.apply)
            self.info = ttk.Label(self, text="", style="Muted.TLabel")
            self.info.pack(anchor="w", pady=(0, 8))
            f, self.tree = make_tree(self, [("loc", "Raf", 80, "center"), ("brand", "Marka", 90, "w"),
                                            ("oem", "OEM", 140, "w"), ("name", "Parça", 230, "w"),
                                            ("sys", "Sistem", 70, "center"), ("cnt", "Sayılan", 70, "center"),
                                            ("diff", "Fark", 70, "center"), ("val", "Fark tutarı", 110, "e")])
            f.pack(fill="both", expand=True)
            self.tree.bind("<Double-1>", lambda e: self.guard(self.enter)())

        def refresh(self):
            c = self.depo.open_count()
            self.count = c
            if not c:
                self.info.configure(text="Açık sayım yok. Raf/koridor önekiyle (ör. 'A' veya 'B-02') ya da tüm depo "
                                         "için yeni sayım başlatın.")
                fill_tree(self.tree, [])
                return
            rows = self.depo.count_lines(c["id"])
            done = sum(r["counted"] is not None for r in rows)
            self.info.configure(text=f"Sayım #{c['id']} · Kapsam: {c['scope']} · Başlangıç: {c['created'][:16]} · "
                                     f"{done}/{len(rows)} parça sayıldı. Satıra çift tıklayıp sayılan miktarı girin.")
            out, tags = [], []
            for r in rows:
                diff = None if r["counted"] is None else r["counted"] - r["system_qty"]
                out.append((r["part_id"], r["location"], r["brand"], r["oem"], r["name"], r["system_qty"],
                            "" if r["counted"] is None else r["counted"], "" if diff is None else f"{diff:+d}",
                            "" if not diff else fmt_money(diff * r["avg_cost"])))
                tags.append("muted" if diff is None else "ok" if diff == 0 else "bad")
            fill_tree(self.tree, out, tags)

        def start(self):
            def save(v):
                self.depo.start_count(self.user, v["scope"])
                self.refresh()
            FormDialog(self, "Yeni sayım", [dict(key="scope", label="Raf öneki (boş = tüm depo)")], save)

        def enter(self):
            if not self.count:
                raise DepoError("Açık sayım yok.")
            pid = selected_id(self.tree, "parça")
            p = self.depo.get_part(pid)

            def save(v):
                self.depo.set_counted(self.user, self.count["id"], pid, v["n"])
                self.refresh()
                kids = list(self.tree.get_children())
                i = kids.index(str(pid))
                if i + 1 < len(kids):
                    self.tree.selection_set(kids[i + 1])
                    self.tree.see(kids[i + 1])
            FormDialog(self, f"{p['location']} · {p['name']}", [dict(key="n", label="Sayılan miktar")], save)

        def apply(self):
            if not self.count:
                raise DepoError("Açık sayım yok.")
            if not messagebox.askyesno("Sayımı uygula", "Sayılan satırlardaki farklar stoğa işlenecek ve sayım "
                                       "kapanacak. Devam edilsin mi?", parent=self):
                return
            changed, value = self.depo.apply_count(self.user, self.count["id"])
            messagebox.showinfo("Sayım uygulandı", f"{changed} parçada stok düzeltildi.\n"
                                                   f"Net fark (maliyet): {fmt_money(value)}", parent=self)
            self.refresh()

    class ReportsPage(Page):
        title = "Raporlar"

        def build(self):
            self.names = {v: k for k, v in Depo.REPORTS.items()}
            self.choice = ttk.Combobox(self.toolbar, values=list(self.names), state="readonly", width=38)
            self.choice.pack(side="left")
            self.choice.current(0)
            self.choice.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            self.btn("CSV olarak kaydet", self.save, accent=True)
            self.cards = ttk.Frame(self)
            self.cards.pack(fill="x", pady=(0, 14))
            self.holder = ttk.Frame(self)
            self.holder.pack(fill="both", expand=True)

        def refresh(self):
            for w in self.cards.winfo_children():
                w.destroy()
            s = self.depo.summary()
            for i, (label, val) in enumerate([("Parça çeşidi", s["parts"]), ("Toplam adet", s["units"]),
                                              ("Stok değeri (maliyet)", fmt_money(s["value"])),
                                              ("Kritik stokta", s["critical"]),
                                              ("Bugünkü çıkış", f"{fmt_money(s['today_out'])} · {s['today_docs']} fiş")]):
                f = ttk.Frame(self.cards, style="Card.TFrame", padding=14)
                f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
                self.cards.columnconfigure(i, weight=1)
                ttk.Label(f, text=label, style="Card.TLabel").pack(anchor="w")
                ttk.Label(f, text=str(val), style="CardBig.TLabel").pack(anchor="w", pady=(4, 0))
            for w in self.holder.winfo_children():
                w.destroy()
            self.data = self.depo.report(self.user, self.names[self.choice.get()])
            headers, rows = self.data
            f, t = make_tree(self.holder, [(f"c{i}", h, 120, "w") for i, h in enumerate(headers)])
            f.pack(fill="both", expand=True)
            fill_tree(t, [(i, *r) for i, r in enumerate(rows)])

        def save(self):
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                                initialfile=self.names[self.choice.get()] + ".csv")
            if path:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f, delimiter=";")
                    w.writerow(self.data[0])
                    w.writerows(self.data[1])

    class UsersPage(Page):
        title = "Kullanıcılar"

        def build(self):
            self.btn("Yeni kullanıcı", self.new, accent=True)
            self.btn("Aktif / pasif", self.toggle)
            self.btn("Şifre sıfırla", self.reset)
            f, self.tree = make_tree(self, [("u", "Kullanıcı adı", 180, "w"), ("r", "Rol", 160, "w"),
                                            ("a", "Durum", 90, "center"), ("c", "Oluşturulma", 160, "w")])
            f.pack(fill="both", expand=True)

        def refresh(self):
            rows = self.depo.list_users()
            fill_tree(self.tree, [(r["id"], r["username"], ROLE_NAMES[r["role"]], "Aktif" if r["active"] else "Pasif",
                                   r["created_at"]) for r in rows], ["" if r["active"] else "muted" for r in rows])

        def new(self):
            roles = {v: k for k, v in ROLE_NAMES.items()}

            def save(v):
                self.depo.add_user(self.user, v["u"], v["p"], roles.get(v["r"], ""))
                self.refresh()
            FormDialog(self, "Yeni kullanıcı", [dict(key="u", label="Kullanıcı adı"),
                                                dict(key="p", label="Geçici şifre", kind="password"),
                                                dict(key="r", label="Rol", kind="combo", values=list(roles),
                                                     default=ROLE_NAMES["tezgah"])], save,
                       note="Kullanıcı ilk girişte şifresini değiştirmek zorundadır.")

        def toggle(self):
            uid = selected_id(self.tree, "kullanıcı")
            row = self.depo.q1("SELECT active FROM users WHERE id=?", (uid,))
            self.depo.set_user_active(self.user, uid, not row["active"])
            self.refresh()

        def reset(self):
            uid = selected_id(self.tree, "kullanıcı")
            FormDialog(self, "Şifre sıfırla", [dict(key="p", label="Yeni geçici şifre", kind="password")],
                       lambda v: self.depo.reset_password(self.user, uid, v["p"]))

    class LogPage(Page):
        title = "İşlem Kaydı"

        def build(self):
            f, self.tree = make_tree(self, [("ts", "Zaman", 150, "w"), ("u", "Kullanıcı", 110, "w"),
                                            ("a", "İşlem", 150, "w"), ("d", "Detay", 400, "w")])
            f.pack(fill="both", expand=True)

        def refresh(self):
            fill_tree(self.tree, [(i, r["ts"], r["username"], r["action"], r["detail"] or "")
                                  for i, r in enumerate(self.depo.audit_log())])

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.depo, self.user = depo, None
            self.title(f"{APP_NAME} {VERSION}")
            self.geometry("1260x760")
            self.minsize(1040, 620)
            setup_style(self)
            self.show_login()

        def clear(self):
            for w in self.winfo_children():
                w.destroy()

        def show_login(self):
            self.clear()
            self.user = None
            outer = ttk.Frame(self, style="Side.TFrame")
            outer.pack(fill="both", expand=True)
            box = ttk.Frame(outer, style="Card.TFrame", padding=36)
            box.place(relx=0.5, rely=0.5, anchor="center")
            ttk.Label(box, text="🔧 OTOPARCA DEPO PRO", style="Card.TLabel", foreground=C["accent2"],
                      font=("Segoe UI", 17, "bold")).pack(pady=(0, 4))
            ttk.Label(box, text="Yedek parça depo ve stok yönetimi", style="Card.TLabel").pack(pady=(0, 20))
            u, p = tk.StringVar(), tk.StringVar()
            for text, var, show in (("Kullanıcı adı", u, ""), ("Şifre", p, "•")):
                ttk.Label(box, text=text, style="Card.TLabel").pack(anchor="w")
                e = ttk.Entry(box, textvariable=var, show=show, width=32)
                e.pack(pady=(2, 10))
                if not show:
                    e.focus_set()

            def do_login(*_):
                try:
                    self.user = self.depo.login(u.get(), p.get())
                except DepoError as ex:
                    messagebox.showerror("Giriş", str(ex), parent=self)
                    return
                if demo and self.user.role == "yonetici":
                    self.depo.seed_demo(self.user)
                if self.user.must_change:
                    self.force_password_change()
                else:
                    self.show_main()
            ttk.Button(box, text="Giriş yap", style="Accent.TButton", command=do_login).pack(fill="x", pady=(6, 0))
            ttk.Label(box, text="İlk giriş: admin / admin123", style="Card.TLabel").pack(pady=(14, 0))
            self.bind("<Return>", do_login)

        def force_password_change(self):
            def save(v):
                if v["n1"] != v["n2"]:
                    raise DepoError("Yeni şifreler eşleşmiyor.")
                self.depo.change_password(self.user, v["old"], v["n1"])
                self.after(50, self.show_main)
            FormDialog(self, "Şifrenizi değiştirin", [dict(key="old", label="Mevcut şifre", kind="password"),
                                                       dict(key="n1", label="Yeni şifre", kind="password"),
                                                       dict(key="n2", label="Yeni şifre (tekrar)", kind="password")],
                       save, note="En az 8 karakter; harf ve rakam içermeli.")

        def show_main(self):
            self.clear()
            self.unbind("<Return>")
            side = ttk.Frame(self, style="Side.TFrame", width=220)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            ttk.Label(side, text="🔧 OTOPARCA\nDEPO PRO", style="Logo.TLabel").pack(anchor="w", padx=18, pady=(20, 4))
            ttk.Label(side, text=f"{self.user.username} · {ROLE_NAMES[self.user.role]}",
                      style="Side.TLabel").pack(anchor="w", padx=18, pady=(0, 18))
            self.content = ttk.Frame(self)
            self.content.pack(side="left", fill="both", expand=True)
            can = lambda perm: self.depo.can(self.user, perm)
            pages = [("Parça arama", PartsPage, True),
                     ("Hareket fişleri", DocsPage, can("giris") or can("cikis") or can("iade")),
                     ("Depo sayımı", CountPage, can("sayim")),
                     ("Raporlar", ReportsPage, can("rapor")),
                     ("Kullanıcılar", UsersPage, can("kullanici")),
                     ("İşlem kaydı", LogPage, self.user.role == "yonetici")]
            self.nav = {}
            for name, cls, visible in pages:
                if visible:
                    b = ttk.Button(side, text=name, style="Nav.TButton", command=lambda c=cls: self.open_page(c))
                    b.pack(fill="x")
                    self.nav[cls] = b
            ttk.Button(side, text="Çıkış yap", style="Nav.TButton", command=self.show_login).pack(side="bottom",
                                                                                                 fill="x", pady=12)
            self.page = None
            self.open_page(PartsPage)

        def open_page(self, cls):
            if self.page:
                self.page.destroy()
            for c, b in self.nav.items():
                b.configure(style="NavActive.TButton" if c is cls else "Nav.TButton")
            self.page = cls(self)
            self.page.pack(fill="both", expand=True)
            try:
                self.page.refresh()
            except DepoError as e:
                messagebox.showerror("Hata", str(e), parent=self)

    App().mainloop()


# ════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME} {VERSION}")
    ap.add_argument("--test", action="store_true", help="birim testlerini çalıştır")
    ap.add_argument("--demo", action="store_true", help="boş veritabanına örnek veri ekle")
    ap.add_argument("--db", default=DEFAULT_DB, help="veritabanı dosyası")
    args = ap.parse_args()
    if args.test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(DepoTests)
        sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
    run_gui(Depo(args.db), demo=args.demo)


if __name__ == "__main__":
    main()
