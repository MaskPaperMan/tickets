"""汽配批发单机版 MVP。仅依赖 Python 标准库。"""
import csv
import html
import os
import shutil
import sqlite3
import sys
import webbrowser
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

APP_NAME = '汽配批发管理系统'
ASSET_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets'
COMPANY_FIELDS = {'company_name': '公司名', 'business_scope': '经营范围',
                  'company_address': '公司地址', 'company_phone': '联系电话'}
FONT_DEFAULTS = {'company_name_font': '25', 'business_scope_font': '18',
                 'company_address_font': '16', 'company_phone_font': '16', 'body_font': '16'}
FONT_SIZES = tuple(str(size) for size in range(8, 37))
DATA_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AutoPartsMVP'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'autoparts.db'
PRINT_DIR = DATA_DIR / 'printouts'
PRINT_DIR.mkdir(exist_ok=True)


def money(value):
    try:
        d = Decimal(str(value).strip())
        if not d.is_finite() or d < 0:
            raise ValueError
        return int((d * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError('金额必须为非负数字')


def fmt(cents):
    return f'{cents / 100:.2f}'


def money_upper(cents):
    """将整数分转换成人民币大写，按万、亿分组处理零位。"""
    if not isinstance(cents, int) or cents < 0:
        raise ValueError('金额必须为非负整数分')
    digits = '零壹贰叁肆伍陆柒捌玖'

    def group_text(number):
        result = ''
        pending_zero = False
        for divisor, unit in [(1000, '仟'), (100, '佰'), (10, '拾'), (1, '')]:
            digit, number = divmod(number, divisor)
            if digit:
                if pending_zero:
                    result += '零'
                result += digits[digit] + unit
                pending_zero = False
            elif result:
                pending_zero = True
        return result

    yuan, fraction = divmod(cents, 100)
    groups = []
    remaining = yuan
    while remaining:
        remaining, group = divmod(remaining, 10000)
        groups.append(group)
    result = ''
    pending_zero = False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            pending_zero = bool(result)
            continue
        if result and (pending_zero or group < 1000):
            result += '零'
        # 每两个万进位为亿，可覆盖 SQLite 整数金额范围。
        group_unit = ('万' if index % 2 else '') + '亿' * (index // 2)
        result += group_text(group) + group_unit
        pending_zero = False
    result = (result or '零') + '元'
    jiao, fen = divmod(fraction, 10)
    if not fraction:
        return result + '整'
    if jiao:
        result += digits[jiao] + '角'
    elif yuan:
        result += '零'
    if fen:
        result += digits[fen] + '分'
    return result


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    try:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=5000')
        with con:
            yield con
    finally:
        con.close()


def init_db():
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sequences(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          unit TEXT NOT NULL DEFAULT '件', sale_price INTEGER NOT NULL DEFAULT 0 CHECK(sale_price>=0),
          purchase_price INTEGER NOT NULL DEFAULT 0 CHECK(purchase_price>=0),
          stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0), min_stock INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS customers(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, phone TEXT DEFAULT '', address TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS purchases(
          id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
          note TEXT DEFAULT '', total INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS purchase_items(
          id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchases(id),
          product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL CHECK(qty>0),
          unit_price INTEGER NOT NULL CHECK(unit_price>=0), amount INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sales(
          id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL REFERENCES customers(id),
          created_at TEXT NOT NULL, total INTEGER NOT NULL, note TEXT DEFAULT '', status TEXT NOT NULL DEFAULT '正常');
        CREATE TABLE IF NOT EXISTS sale_items(
          id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id),
          product_id INTEGER NOT NULL REFERENCES products(id), sku TEXT NOT NULL, product_name TEXT NOT NULL,
          unit TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0), unit_price INTEGER NOT NULL,
          amount INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id),
          created_at TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS inventory_movements(
          id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
          created_at TEXT NOT NULL, change_qty INTEGER NOT NULL,
          source_type TEXT NOT NULL, source_id INTEGER NOT NULL, note TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
        CREATE INDEX IF NOT EXISTS idx_payment_sale ON payments(sale_id);
        CREATE INDEX IF NOT EXISTS idx_moves_product ON inventory_movements(product_id);
        ''')


def order_number(prefix):
    return prefix + datetime.now().strftime('%Y%m%d%H%M%S%f')


def load_settings():
    values = dict.fromkeys(COMPANY_FIELDS, '')
    values.update(FONT_DEFAULTS)
    values.update({r['key']: r['value'] for r in query('SELECT key,value FROM settings')
                   if r['key'] in values})
    for key, default in FONT_DEFAULTS.items():
        if values[key] not in FONT_SIZES:
            values[key] = default
    return values


def save_settings(values):
    fonts = {}
    for key in FONT_DEFAULTS:
        if key in values:
            fonts[key] = str(values[key]).strip()
            if fonts[key] not in FONT_SIZES:
                raise ValueError('字号请选择8至36磅之间的整数')
    with db() as c:
        c.executemany('INSERT INTO settings(key,value) VALUES(?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      [(key, values.get(key, '').strip()) for key in COMPANY_FIELDS] + list(fonts.items()))


def create_product(name, unit='件', price=0, cost=0, minimum=0):
    name = name.strip()
    if not name:
        raise ValueError('商品名称不能为空')
    if unit not in ('个', '件'):
        raise ValueError('单位请选择“个”或“件”')
    if price < 0 or cost < 0 or minimum < 0:
        raise ValueError('价格和预警库存不能为负数')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT OR IGNORE INTO sequences(name,value) VALUES('product_sku',0)")
        number = c.execute("SELECT value FROM sequences WHERE name='product_sku'").fetchone()[0]
        while True:
            number += 1
            sku = f'SP{number:06d}'
            if not c.execute('SELECT 1 FROM products WHERE sku=?', (sku,)).fetchone():
                break
        c.execute('INSERT INTO products(sku,name,unit,sale_price,purchase_price,min_stock) '
                  'VALUES(?,?,?,?,?,?)', (sku, name, unit, price, cost, minimum))
        c.execute("UPDATE sequences SET value=? WHERE name='product_sku'", (number,))
    return sku


def update_product(product_id, name, unit, price):
    name = name.strip()
    if not name:
        raise ValueError('商品名称不能为空')
    if unit not in ('个', '件') or price < 0:
        raise ValueError('单位请选择个或件，销售价不能为负数')
    with db() as c:
        if not c.execute('UPDATE products SET name=?,unit=?,sale_price=? WHERE id=? AND active=1',
                         (name, unit, price, product_id)).rowcount:
            raise ValueError('商品不存在或已删除')


def apply_purchase_changes(c, deltas, source_id, reason):
    for product_id, delta in deltas.items():
        p = c.execute('SELECT name,stock FROM products WHERE id=?', (product_id,)).fetchone()
        if p['stock'] + delta < 0:
            raise ValueError(f"{p['name']}库存不足：现有{p['stock']}，需扣回{-delta}，本次操作未保存")
    for product_id, delta in deltas.items():
        c.execute('UPDATE products SET stock=stock+? WHERE id=?', (delta, product_id))
        if delta:
            c.execute('INSERT INTO inventory_movements(product_id,created_at,change_qty,source_type,source_id) VALUES(?,?,?,?,?)',
                      (product_id, now(), delta, reason, source_id))


def refresh_purchase_costs(c, product_ids):
    for product_id in product_ids:
        latest = c.execute('SELECT unit_price FROM purchase_items WHERE product_id=? ORDER BY purchase_id DESC,id DESC LIMIT 1',
                           (product_id,)).fetchone()
        c.execute('UPDATE products SET purchase_price=? WHERE id=?', (latest[0] if latest else 0, product_id))


def update_purchase(item_id, product_id, qty, price):
    if qty <= 0 or price < 0:
        raise ValueError('数量必须大于0，进价不能为负数')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM purchase_items WHERE id=?', (item_id,)).fetchone()
        if not old:
            raise ValueError('入库记录不存在或已删除')
        if not c.execute('SELECT 1 FROM products WHERE id=? AND active=1', (product_id,)).fetchone():
            raise ValueError('请选择未删除的商品')
        deltas = {old['product_id']: -old['qty']}
        deltas[product_id] = deltas.get(product_id, 0) + qty
        apply_purchase_changes(c, deltas, old['purchase_id'], '采购修改')
        c.execute('UPDATE purchase_items SET product_id=?,qty=?,unit_price=?,amount=? WHERE id=?',
                  (product_id, qty, price, qty * price, item_id))
        c.execute('UPDATE purchases SET total=(SELECT SUM(amount) FROM purchase_items WHERE purchase_id=?) WHERE id=?',
                  (old['purchase_id'], old['purchase_id']))
        refresh_purchase_costs(c, deltas)


def delete_purchases(item_ids):
    item_ids = list(dict.fromkeys(item_ids))
    if not item_ids:
        raise ValueError('请先勾选入库记录')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = []
        deltas = {}
        for item_id in item_ids:
            row = c.execute('SELECT * FROM purchase_items WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('有入库记录不存在或已删除，本次操作未保存')
            rows.append(row)
            deltas[row['product_id']] = deltas.get(row['product_id'], 0) - row['qty']
        # 先验证所有商品的合计扣回数量，随后在同一事务中逐条记录流水。
        for product_id, delta in deltas.items():
            p = c.execute('SELECT name,stock FROM products WHERE id=?', (product_id,)).fetchone()
            if p['stock'] + delta < 0:
                raise ValueError(f"{p['name']}库存不足：现有{p['stock']}，需扣回{-delta}，本次未删除任何记录")
        for row in rows:
            apply_purchase_changes(c, {row['product_id']: -row['qty']}, row['purchase_id'], '采购删除')
            c.execute('DELETE FROM purchase_items WHERE id=?', (row['id'],))
        for purchase_id in {row['purchase_id'] for row in rows}:
            total = c.execute('SELECT SUM(amount) FROM purchase_items WHERE purchase_id=?', (purchase_id,)).fetchone()[0]
            if total is None:
                # 保留单号占位，避免新采购复用已有库存流水的来源 ID。
                c.execute('UPDATE purchases SET total=0 WHERE id=?', (purchase_id,))
            else:
                c.execute('UPDATE purchases SET total=? WHERE id=?', (total, purchase_id))
        refresh_purchase_costs(c, deltas)


def update_product_price(product_id, price):
    if price < 0:
        raise ValueError('销售价不能为负数')
    with db() as c:
        if not c.execute('UPDATE products SET sale_price=? WHERE id=? AND active=1',
                         (price, product_id)).rowcount:
            raise ValueError('商品不存在或已删除')


def update_customer(customer_id, name, phone='', address=''):
    name = name.strip()
    if not name:
        raise ValueError('请输入客户名称')
    try:
        with db() as c:
            if not c.execute('UPDATE customers SET name=?,phone=?,address=? WHERE id=?',
                             (name, phone.strip(), address.strip(), customer_id)).rowcount:
                raise ValueError('客户不存在')
    except sqlite3.IntegrityError:
        raise ValueError('客户名称已存在') from None


def delete_product(product_id):
    """停用零库存商品，保留货号、单据和库存流水。"""
    delete_products([product_id])


def delete_products(product_ids):
    product_ids = list(dict.fromkeys(product_ids))
    if not product_ids:
        raise ValueError('请先勾选要删除的商品')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        errors = []
        for product_id in product_ids:
            product = c.execute('SELECT sku,name,stock FROM products WHERE id=? AND active=1',
                            (product_id,)).fetchone()
            if not product:
                errors.append(f'商品 ID {product_id} 不存在或已删除')
            elif product['stock'] != 0:
                errors.append(f"{product['sku']} {product['name']}仍有库存（{product['stock']}）")
        if errors:
            raise ValueError('本次未删除任何商品：\n' + '\n'.join(errors))
        c.executemany('UPDATE products SET active=0 WHERE id=?', [(pid,) for pid in product_ids])


def delete_customer(customer_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM customers WHERE id=?', (customer_id,)).fetchone():
            raise ValueError('客户不存在或已删除')
        if c.execute('SELECT 1 FROM sales WHERE customer_id=?', (customer_id,)).fetchone():
            raise ValueError('该客户已有销售记录，不能删除，以保留历史单据和欠款信息')
        c.execute('DELETE FROM customers WHERE id=?', (customer_id,))


def create_customer(name, phone='', address=''):
    name = name.strip()
    if not name:
        raise ValueError('请输入客户名称')
    try:
        with db() as c:
            return c.execute('INSERT INTO customers(name,phone,address) VALUES(?,?,?)',
                             (name, phone.strip(), address.strip())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError('客户名称已存在，请选择已有客户') from None


def create_purchase(product_id, qty, price, note=''):
    if qty <= 0 or price < 0:
        raise ValueError('数量必须大于0，金额不能为负')
    with db() as c:
        p = c.execute('SELECT id FROM products WHERE id=? AND active=1', (product_id,)).fetchone()
        if not p:
            raise ValueError('商品不存在')
        total = qty * price
        cur = c.execute('INSERT INTO purchases(number,created_at,note,total) VALUES(?,?,?,?)',
                        (order_number('CG'), now(), note, total))
        pid = cur.lastrowid
        c.execute('INSERT INTO purchase_items(purchase_id,product_id,qty,unit_price,amount) VALUES(?,?,?,?,?)',
                  (pid, product_id, qty, price, total))
        c.execute('UPDATE products SET stock=stock+?, purchase_price=? WHERE id=?', (qty, price, product_id))
        c.execute('INSERT INTO inventory_movements(product_id,created_at,change_qty,source_type,source_id,note) VALUES(?,?,?,?,?,?)',
                  (product_id, now(), qty, '采购入库', pid, note))
        return pid


def create_sale(customer_id, lines, paid=0, note=''):
    if not lines:
        raise ValueError('请至少添加一件商品')
    if paid < 0:
        raise ValueError('收款不能为负数')
    # 单机SQLite事务将销售、库存、收款合并提交，任意失败全部回滚。
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT id FROM customers WHERE id=?', (customer_id,)).fetchone():
            raise ValueError('客户不存在')
        prepared = []
        combined = {}
        for product_id, qty, unit_price in lines:
            if qty <= 0 or unit_price < 0:
                raise ValueError('数量必须大于0，价格不能为负')
            p = c.execute('SELECT * FROM products WHERE id=? AND active=1', (product_id,)).fetchone()
            if not p:
                raise ValueError('有商品不存在或已停用')
            prepared.append((p, qty, unit_price, qty * unit_price))
            combined[product_id] = combined.get(product_id, 0) + qty
        for product_id, qty in combined.items():
            available = c.execute('SELECT stock,name FROM products WHERE id=?', (product_id,)).fetchone()
            if available['stock'] < qty:
                raise ValueError(f"{available['name']}库存不足：现有{available['stock']}，需{qty}")
        total = sum(line[3] for line in prepared)
        if paid > total:
            raise ValueError('本次收款不能大于应收金额')
        cur = c.execute('INSERT INTO sales(number,customer_id,created_at,total,note) VALUES(?,?,?,?,?)',
                        (order_number('XS'), customer_id, now(), total, note))
        sale_id = cur.lastrowid
        for p, qty, price, amount in prepared:
            c.execute('INSERT INTO sale_items(sale_id,product_id,sku,product_name,unit,qty,unit_price,amount) VALUES(?,?,?,?,?,?,?,?)',
                      (sale_id, p['id'], p['sku'], p['name'], p['unit'], qty, price, amount))
            c.execute('UPDATE products SET stock=stock-? WHERE id=?', (qty, p['id']))
            c.execute('INSERT INTO inventory_movements(product_id,created_at,change_qty,source_type,source_id) VALUES(?,?,?,?,?)',
                      (p['id'], now(), -qty, '销售出库', sale_id))
        if paid:
            c.execute('INSERT INTO payments(sale_id,created_at,amount,note) VALUES(?,?,?,?)',
                      (sale_id, now(), paid, '销售时收款'))
        return sale_id


def receive_payment(sale_id, amount):
    if amount <= 0:
        raise ValueError('收款金额必须大于0')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('''SELECT s.total-COALESCE(SUM(p.amount),0) as due FROM sales s
                         LEFT JOIN payments p ON p.sale_id=s.id WHERE s.id=? GROUP BY s.id''', (sale_id,)).fetchone()
        if not r or amount > r['due']:
            raise ValueError('单据不存在，或收款超过剩余欠款')
        c.execute('INSERT INTO payments(sale_id,created_at,amount,note) VALUES(?,?,?,?)',
                  (sale_id, now(), amount, '后续收款'))


def query(sql, params=()):
    with db() as c:
        return c.execute(sql, params).fetchall()


def matching_options(options, keyword, search_text):
    terms = keyword.strip().casefold().split()
    return [label for label in options
            if all(term in search_text.get(label, label).casefold() for term in terms)]


class SearchSelect(ttk.Frame):
    """一体式搜索下拉框；点击列出全部选项，输入后匹配。"""
    def __init__(self, parent, width):
        super().__init__(parent)
        self.options = []
        self.search_text = {}
        self.matches = []
        self.popup = None
        self.pending = None
        self.focus_pending = None
        self.setting = False
        self.value = tk.StringVar(self)
        self.entry = ttk.Entry(self, textvariable=self.value, width=width + 2)
        self.entry.pack(side='left', fill='x', expand=True)
        self.value.trace_add('write', self.edited)
        self.entry.bind('<Button-1>', lambda _: self.after_idle(self.show_all))
        self.entry.bind('<Down>', self.move_down)
        self.entry.bind('<Up>', self.move_up)
        self.entry.bind('<Return>', self.accept)
        self.entry.bind('<Escape>', self.hide)
        self.entry.bind('<FocusOut>', self.schedule_focus_check)
        self.bind('<Destroy>', self.cleanup)
        self.parent_window = self.winfo_toplevel()
        self.configure_binding = self.parent_window.bind('<Configure>', self.parent_moved, add='+')

    def focus_set(self):
        self.entry.focus_set()

    def get(self):
        return self.value.get()

    def set(self, value):
        self.setting = True
        try:
            self.value.set(value)
        finally:
            self.setting = False
        self.hide()

    def set_options(self, options, search_text=None):
        self.options = list(options)
        self.search_text = search_text or {}
        self.hide()

    def edited(self, *_):
        if self.setting:
            return
        if self.pending:
            self.after_cancel(self.pending)
        self.event_generate('<<SearchEdited>>')
        self.pending = self.after(180, self.show_results)

    def show_all(self):
        self.entry.focus_set()
        self.show_results(all_options=True)

    def show_results(self, all_options=False):
        if self.pending:
            self.after_cancel(self.pending)
        self.pending = None
        if self.focus_get() != self.entry:
            return
        keyword = '' if all_options or self.get() in self.options else self.get()
        self.matches = matching_options(self.options, keyword, self.search_text)
        if self.popup is None:
            self.popup = tk.Toplevel(self)
            self.popup.withdraw()
            self.popup.overrideredirect(True)
            self.popup.transient(self.winfo_toplevel())
            border = ttk.Frame(self.popup, borderwidth=1, relief='solid')
            border.pack(fill='both', expand=True)
            self.results = tk.Listbox(border, exportselection=False, activestyle='dotbox',
                                      font='TkDefaultFont', takefocus=False)
            self.results.pack(side='left', fill='both', expand=True)
            scroll = ttk.Scrollbar(border, command=self.results.yview)
            scroll.pack(side='right', fill='y')
            self.results.configure(yscrollcommand=scroll.set)
            self.results.bind('<ButtonRelease-1>', self.accept)
            self.results.bind('<Return>', self.accept)
            self.results.bind('<Escape>', self.hide)
            self.results.bind('<FocusOut>', self.schedule_focus_check)
        self.results.delete(0, 'end')
        for label in self.matches or ['无匹配结果，请更换关键词']:
            self.results.insert('end', label)
        self.results.configure(height=min(max(len(self.matches), 1), 10))
        if self.matches:
            self.results.selection_set(0)
            self.results.activate(0)
        self.popup.update_idletasks()
        width = self.entry.winfo_width()
        height = self.popup.winfo_reqheight()
        x = max(0, min(self.winfo_rootx(), self.winfo_screenwidth() - width))
        y = self.entry.winfo_rooty() + self.entry.winfo_height() - 1
        if y + height > self.winfo_screenheight():
            y = max(0, self.winfo_rooty() - height)
        self.popup.geometry(f'{width}x{height}+{x}+{y}')
        self.popup.deiconify()
        self.popup.lift()

    def move(self, direction):
        if self.popup is None or not self.popup.winfo_viewable():
            self.show_results()
        elif self.matches:
            selected = self.results.curselection()
            index = max(0, min((selected[0] if selected else 0) + direction, len(self.matches) - 1))
            self.results.selection_clear(0, 'end')
            self.results.selection_set(index)
            self.results.activate(index)
            self.results.see(index)
        return 'break'

    def move_down(self, _=None):
        return self.move(1)

    def move_up(self, _=None):
        return self.move(-1)

    def accept(self, _=None):
        if self.popup is None or not self.popup.winfo_viewable():
            self.show_results()
        if self.matches and self.popup is not None:
            selected = self.results.curselection()
            if selected:
                self.set(self.matches[selected[0]])
                self.entry.focus_set()
                self.entry.icursor('end')
                self.event_generate('<<ComboboxSelected>>')
        return 'break'

    def hide(self, _=None):
        if self.pending:
            self.after_cancel(self.pending)
            self.pending = None
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.withdraw()
        return 'break'

    def schedule_focus_check(self, _=None):
        if self.focus_pending:
            self.after_cancel(self.focus_pending)
        self.focus_pending = self.after(120, self.hide_if_unfocused)

    def hide_if_unfocused(self):
        self.focus_pending = None
        if self.focus_get() not in (self.entry, getattr(self, 'results', None)):
            self.hide()

    def parent_moved(self, event):
        if event.widget == self.winfo_toplevel():
            self.hide()

    def cleanup(self, event):
        if event.widget == self:
            self.hide()
            if self.focus_pending:
                self.after_cancel(self.focus_pending)
            self.parent_window.unbind('<Configure>', self.configure_binding)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        icon_path = ASSET_DIR / 'app-icon.png'
        if icon_path.is_file():
            self.app_icon = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(True, self.app_icon)
        if sys.platform == 'win32' and (ASSET_DIR / 'app-icon.ico').is_file():
            self.iconbitmap(str(ASSET_DIR / 'app-icon.ico'))
        self.geometry('1100x700')
        self.minsize(960, 580)
        self.cart = []
        self.product_choices = {}
        self.customer_choices = {}
        style = ttk.Style(self)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill='both', expand=True, padx=12, pady=12)
        self.frames = {}
        for name in ['销售开单', '商品库存', '客户管理', '账款管理', '销售记录', '系统设置']:
            f = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(f, text=name)
            self.frames[name] = f
        self.build_sale()
        self.build_products()
        self.build_purchase()
        self.build_customers()
        self.build_debts()
        self.build_history()
        self.build_settings()
        footer = ttk.Frame(self)
        footer.pack(fill='x', padx=16, pady=(0, 10))
        ttk.Label(footer, text=f'数据库：{DB_PATH}', foreground='#666666').pack(side='left')
        ttk.Button(footer, text='备份数据库', command=self.backup).pack(side='right')
        self.refresh_all()

    def tree(self, parent, columns, headings, height=12):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, pady=8)
        tree = ttk.Treeview(frame, columns=columns, show='headings', height=height, selectmode='browse')
        for col, title, width in headings:
            tree.heading(col, text=title)
            tree.column(col, width=width, minwidth=65)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(frame, command=tree.yview)
        sb.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        tree.configure(yscrollcommand=sb.set, xscrollcommand=horizontal.set)
        return tree

    def formline(self, parent, fields):
        line = ttk.Frame(parent)
        line.pack(fill='x', pady=5)
        controls = {}
        for label, width in fields:
            ttk.Label(line, text=label).pack(side='left', padx=(4, 4))
            ent = ttk.Entry(line, width=width)
            ent.pack(side='left', padx=(0, 10))
            controls[label] = ent
        return line, controls

    def page_toolbar(self, parent, buttons=(), refresh=False):
        bar = ttk.Frame(parent)
        bar.pack(fill='x', pady=(4, 8))
        for label, command in buttons:
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=(0, 8))
        if refresh:
            ttk.Button(bar, text='刷新', command=self.refresh_all).pack(side='right')
        return bar

    def build_products(self):
        f = self.frames['商品库存']
        self.page_toolbar(f, [('新增', self.add_product), ('入库记录', self.open_purchase_history)], refresh=True)
        search = ttk.Frame(f)
        search.pack(fill='x', pady=6)
        ttk.Label(search, text='搜索商品').pack(side='left')
        self.product_search = tk.StringVar()
        ttk.Entry(search, textvariable=self.product_search, width=36).pack(side='left', padx=5)
        self.product_search.trace_add('write', lambda *_: self.refresh_all())
        self.product_tree = self.tree(f, ['id','sku','name','unit','cost','price','stock','minstock','purchase'],
            [('id','ID',45),('sku','货号',100),('name','商品名称',190),('unit','单位',70),('cost','参考进价',90),('price','销售价',90),('stock','库存',80),('minstock','预警值',80),('purchase','操作',210)])
        self.checked_product_ids = set()
        self.product_tree.configure(show='tree headings', selectmode='extended')
        self.product_tree.column('#0', width=65, minwidth=65, stretch=False)
        self.product_tree.heading('#0', text='☐ 全选', command=self.toggle_all_products)
        self.product_tree.bind('<Button-1>', self.toggle_product_check)
        self.product_tree.bind('<<TreeviewSelect>>', self.sync_product_checks)
        self.product_tree.bind('<space>', self.toggle_focused_product)
        self.row_buttons(self.product_tree, 'purchase', [
            ('采购', lambda row: self.open_product_purchase(int(row))),
            ('修改', lambda row: self.product_action(row, self.edit_product)),
            ('删除', lambda row: self.product_action(row, self.do_delete_product))])

    def product_action(self, row, action):
        self.product_tree.selection_set(row)
        action()

    def row_buttons(self, tree, column, actions, action_filter=None):
        """仅为可见行放置真实按钮；滚动后按单元格位置重新排布。"""
        # 使用紧凑边框，避免 macOS 原生按钮隐藏的左右留白。
        style = ttk.Style(tree)
        if 'Compact.border' not in style.element_names():
            style.element_create('Compact.border', 'from', 'clam', 'Button.border')
        style.layout('Compact.TButton', [('Compact.border', {'sticky': 'nswe', 'children': [
            ('Button.padding', {'sticky': 'nswe', 'children': [('Button.label', {'sticky': 'nswe'})]})]})])
        style.configure('Compact.TButton', padding=(5, 2), borderwidth=1, relief='raised',
                        background='#f7f7f7', foreground='#222222', anchor='center')
        style.map('Compact.TButton', background=[('pressed', '#dce6ef'), ('active', '#edf3f8')])
        probe = ttk.Button(tree, text=max((label for label, _ in actions), key=len), width=0, style='Compact.TButton')
        probe.update_idletasks()
        button_width = probe.winfo_reqwidth()
        button_height = probe.winfo_reqheight()
        probe.destroy()
        row_height = max(32, button_height + 8)
        style_name = 'Action' + column + '.Treeview'
        ttk.Style(tree).configure(style_name, rowheight=row_height)
        tree.configure(style=style_name)
        action_width = len(actions) * button_width + max(0, len(actions)-1) * 2 + 8
        tree.column(column, width=action_width, minwidth=action_width, stretch=False)
        frames = {}
        tree.action_frames = frames
        def arrange():
            if not tree.winfo_exists():
                return
            visible = {}
            if tree.winfo_viewable():
                for row in tree.get_children():
                    box = tree.bbox(row, column)
                    if box:
                        visible[row] = box
            for row in list(frames):
                if row not in visible:
                    frames.pop(row).destroy()
            for row, (x, y, width, height) in visible.items():
                row_actions = [(label, command) for label, command in actions
                               if action_filter is None or action_filter(row, label)]
                labels = tuple(label for label, _ in row_actions)
                if row in frames and frames[row].action_labels != labels:
                    frames.pop(row).destroy()
                if row not in frames:
                    frame = ttk.Frame(tree)
                    frames[row] = frame
                    frame.action_labels = labels
                    for index, (label, command) in enumerate(row_actions):
                        ttk.Button(frame, text=label, width=0, style='Compact.TButton',
                                   command=lambda r=row, c=command: c(r)).pack(side='left', fill='y', padx=(0 if index == 0 else 2, 0))
                frames[row].place(x=x+4, y=y+4, width=max(1, width-8), height=height-8)
            tree.after(150, arrange)
        arrange()

    def sales_search(self, parent, show_due_filter=False):
        line = ttk.Frame(parent)
        line.pack(fill='x', pady=6)
        ttk.Label(line, text='搜索客户、电话、单号或日期').pack(side='left')
        value = tk.StringVar()
        ttk.Entry(line, textvariable=value, width=38).pack(side='left', padx=6)
        ttk.Button(line, text='清空', command=lambda: value.set('')).pack(side='left')
        if show_due_filter:
            ttk.Checkbutton(line, text='仅看欠款记录', variable=self.only_due,
                            command=self.refresh_sales).pack(side='left', padx=8)
        value.trace_add('write', lambda *_: self.refresh_sales())
        return value

    def edit_dialog(self, title, fields, on_save):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        content = ttk.Frame(dialog, padding=16)
        content.pack(fill='both', expand=True)
        controls = {}
        for row, (label, value, choices) in enumerate(fields):
            ttk.Label(content, text=label).grid(row=row, column=0, sticky='w', pady=6)
            if choices is None:
                control = ttk.Entry(content, width=40)
                control.insert(0, value)
            else:
                control = SearchSelect(content, width=40)
                control.set_options(choices)
                control.set(value)
            control.grid(row=row, column=1, padx=8, pady=6)
            controls[label] = control

        def save(_=None):
            try:
                on_save({key: control.get() for key, control in controls.items()})
            except (ValueError, sqlite3.Error) as ex:
                messagebox.showerror('保存失败', str(ex), parent=dialog)
                return
            dialog.destroy()

        buttons = ttk.Frame(content)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='保存', command=save).pack(side='left', padx=5)
        ttk.Button(buttons, text='取消', command=dialog.destroy).pack(side='left')
        dialog.bind('<Return>', save)
        dialog.bind('<Escape>', lambda _: dialog.destroy())
        dialog.grab_set()
        next(iter(controls.values())).focus_set()
        return dialog

    def edit_product(self):
        selected = self.product_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning('请选择一个商品', '修改时请只选择一个商品')
            return
        product_id = int(self.product_tree.item(selected[0], 'values')[0])
        product = query('SELECT * FROM products WHERE id=?', (product_id,))[0]

        def save(values):
            update_product(product_id, values['商品名称'], values['单位'], money(values['销售价（元）']))
            self.refresh_all()
            if self.product_choices.get(self.sale_product.get()) == product_id:
                self.fill_sale_price()

        self.edit_dialog(f"修改商品 · {product['sku']}",
                         [('商品名称', product['name'], None), ('单位', product['unit'], ('个', '件')),
                          ('销售价（元）', fmt(product['sale_price']), None)], save)

    def sync_product_checks(self, _=None):
        self.checked_product_ids = set(self.product_tree.selection())
        rows = self.product_tree.get_children()
        for row in rows:
            self.product_tree.item(row, text='☑' if row in self.checked_product_ids else '☐')
        all_checked = bool(rows) and len(self.checked_product_ids) == len(rows)
        self.product_tree.heading('#0', text='☑ 全选' if all_checked else '☐ 全选')

    def toggle_product_row(self, row):
        if row in self.product_tree.selection():
            self.product_tree.selection_remove(row)
        else:
            self.product_tree.selection_add(row)
        self.product_tree.focus(row)
        self.sync_product_checks()

    def toggle_product_check(self, event):
        if (self.product_tree.identify_region(event.x, event.y) in ('tree', 'cell')
                and self.product_tree.identify_column(event.x) == '#0'):
            row = self.product_tree.identify_row(event.y)
            if row:
                self.toggle_product_row(row)
                return 'break'

    def toggle_focused_product(self, _=None):
        row = self.product_tree.focus()
        if row:
            self.toggle_product_row(row)
        return 'break'

    def toggle_all_products(self):
        rows = self.product_tree.get_children()
        if rows and len(self.product_tree.selection()) == len(rows):
            self.product_tree.selection_remove(*rows)
        else:
            self.product_tree.selection_set(rows)
        self.sync_product_checks()

    def add_product(self):
        def save(values):
            try:
                minimum = int(values['预警库存'].strip() or '0')
            except ValueError:
                raise ValueError('预警库存请输入非负整数') from None
            create_product(values['商品名称'], values['单位'],
                           money(values['销售价（元）'].strip() or '0'),
                           money(values['参考进价（元）'].strip() or '0'), minimum)
            self.product_search.set('')
            self.refresh_all()

        return self.edit_dialog('新增商品（货号自动生成）',
                                [('商品名称', '', None), ('单位', '件', ('个', '件')),
                                 ('销售价（元）', '0.00', None), ('参考进价（元）', '0.00', None),
                                 ('预警库存', '0', None)], save)

    def do_delete_product(self):
        selected = self.product_tree.selection()
        if not selected:
            messagebox.showwarning('未选择', '请先勾选要删除的商品')
            return
        products = [self.product_tree.item(row, 'values') for row in selected]
        product_ids = {int(values[0]) for values in products}
        blocked = [f'{values[1]} {values[2]}' for values in products
                   if any(line[0] == int(values[0]) for line in self.cart)]
        if blocked:
            messagebox.showwarning('无法删除', '以下商品已加入销售清单，请先移除：\n' + '\n'.join(blocked))
            return
        if not messagebox.askyesno('删除商品', f'确定删除勾选的 {len(product_ids)} 个商品？\n历史单据和流水会保留。', parent=self):
            return
        try:
            delete_products(product_ids)
            self.refresh_all()
        except (ValueError, sqlite3.Error) as ex:
            messagebox.showerror('删除失败', str(ex))

    def build_settings(self):
        f = self.frames['系统设置']
        ttk.Label(f, text='保存后用于销售单打印，重新打开程序仍会保留。').pack(anchor='w', pady=8)
        self.settings_entries = {}
        values = load_settings()
        form = ttk.Frame(f)
        form.pack(fill='x')
        ttk.Label(form, text='公司信息').grid(row=0, column=1, sticky='w', padx=8, pady=6)
        ttk.Label(form, text='字号（磅）').grid(row=0, column=2, sticky='w', padx=8, pady=6)
        for row, (key, label) in enumerate(COMPANY_FIELDS.items(), 1):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', pady=6)
            entry = ttk.Entry(form, width=65)
            entry.insert(0, values[key])
            entry.grid(row=row, column=1, sticky='ew', padx=8, pady=6)
            self.settings_entries[key] = entry
            selector = SearchSelect(form, width=8)
            selector.set_options(FONT_SIZES)
            selector.set(values[key + '_font'])
            selector.grid(row=row, column=2, sticky='w', padx=8, pady=6)
            self.settings_entries[key + '_font'] = selector
        ttk.Label(form, text='正文').grid(row=5, column=0, sticky='w', pady=6)
        ttk.Label(form, text='客户信息、商品表格、金额、签名等').grid(row=5, column=1, sticky='w', padx=8)
        selector = SearchSelect(form, width=8)
        selector.set_options(FONT_SIZES)
        selector.set(values['body_font'])
        selector.grid(row=5, column=2, sticky='w', padx=8, pady=6)
        self.settings_entries['body_font'] = selector
        ttk.Button(f, text='保存设置', command=self.do_save_settings).pack(anchor='w', pady=12)

    def do_save_settings(self):
        try:
            save_settings({key: entry.get() for key, entry in self.settings_entries.items()})
            messagebox.showinfo('保存成功', '公司信息已保存，将用于后续打印。')
        except (ValueError, sqlite3.Error) as ex:
            messagebox.showerror('保存失败', str(ex))

    def build_customers(self):
        f = self.frames['客户管理']
        self.page_toolbar(f, [('新增客户', self.add_customer)], refresh=True)
        search = ttk.Frame(f)
        search.pack(fill='x', pady=6)
        ttk.Label(search, text='搜索客户名称、电话或地址').pack(side='left')
        self.customer_search = tk.StringVar()
        ttk.Entry(search, textvariable=self.customer_search, width=38).pack(side='left', padx=6)
        ttk.Button(search, text='清空', command=lambda: self.customer_search.set('')).pack(side='left')
        self.customer_search.trace_add('write', lambda *_: self.refresh_all())
        self.customer_tree = self.tree(f, ['id','name','phone','address','actions'],
            [('id','ID',60),('name','客户名称',210),('phone','联系电话',170),('address','客户地址',380),('actions','操作',120)])
        self.row_buttons(self.customer_tree, 'actions', [
            ('修改', lambda row: self.customer_action(row, self.edit_customer)),
            ('删除', lambda row: self.customer_action(row, self.do_delete_customer))])

    def customer_action(self, row, action):
        self.customer_tree.selection_set(row)
        action()

    def do_delete_customer(self):
        selected = self.customer_tree.selection()
        if not selected:
            return
        values = self.customer_tree.item(selected[0], 'values')
        if not messagebox.askyesno('删除客户', f'确定删除客户“{values[1]}”？', parent=self):
            return
        try:
            delete_customer(int(values[0]))
            self.refresh_all()
        except (ValueError, sqlite3.Error) as ex:
            messagebox.showerror('删除失败', str(ex), parent=self)

    def edit_customer(self):
        selected = self.customer_tree.selection()
        if not selected:
            messagebox.showwarning('未选择', '请先选择要修改的客户')
            return
        values = self.customer_tree.item(selected[0], 'values')
        customer_id = int(values[0])

        def save(fields):
            update_customer(customer_id, fields['客户名称'], fields['电话'], fields['地址'])
            self.refresh_all()

        self.edit_dialog('修改客户', [('客户名称', values[1], None),
                                    ('电话', values[2], None), ('地址', values[3], None)], save)

    def add_customer(self):
        def save(values):
            create_customer(values['客户名称'], values['电话'], values['地址'])
            self.refresh_all()
        return self.edit_dialog('新增客户', [('客户名称', '', None), ('电话', '', None), ('地址', '', None)], save)

    def open_product_purchase(self, product_id):
        products = query('SELECT * FROM products WHERE id=? AND active=1', (product_id,))
        if not products:
            messagebox.showwarning('商品不存在', '请刷新商品列表')
            return
        product = products[0]

        def save(values):
            try:
                qty = int(values['数量'].strip())
            except ValueError:
                raise ValueError('请输入大于0的整数数量') from None
            create_purchase(product_id, qty, money(values['进价（元）']))
            self.refresh_all()

        return self.edit_dialog(f"采购入库 · {product['sku']} · {product['name']}",
                                [('数量', '', None), ('进价（元）', fmt(product['purchase_price']), None)], save)

    def open_purchase_history(self):
        self.refresh_purchase_history()
        self.purchase_window.deiconify()
        self.purchase_window.lift()

    def build_purchase(self):
        self.purchase_window = tk.Toplevel(self)
        self.purchase_window.withdraw()
        self.purchase_window.title('入库记录')
        self.purchase_window.geometry('1100x600')
        self.purchase_window.transient(self)
        self.purchase_window.protocol('WM_DELETE_WINDOW', self.purchase_window.withdraw)
        f = ttk.Frame(self.purchase_window, padding=12)
        f.pack(fill='both', expand=True)
        line = ttk.Frame(f)
        line.pack(fill='x')
        ttk.Label(line, text='筛选商品').pack(side='left')
        self.purchase_product = SearchSelect(line, width=42)
        self.purchase_product.pack(side='left', padx=6)
        ttk.Button(line, text='筛选', command=self.refresh_purchase_history).pack(side='left')
        ttk.Button(line, text='全部记录', command=self.clear_purchase_filter).pack(side='left', padx=5)
        actions = ttk.Frame(f)
        actions.pack(fill='x', pady=4)
        ttk.Button(actions, text='修改', command=self.edit_purchase).pack(side='left')
        ttk.Button(actions, text='删除', command=self.do_delete_purchase).pack(side='left', padx=5)
        ttk.Label(actions, text='修改或删除自动调整库存，库存不足时不能操作。').pack(side='left', padx=10)
        self.purchase_tree = self.tree(f, ['number','time','sku','name','qty','price','total'],
            [('number','采购单号',205),('time','时间',150),('sku','货号',100),('name','商品',220),('qty','数量',80),('price','单价',90),('total','金额',100)])
        self.purchase_tree.configure(show='tree headings', selectmode='extended')
        self.purchase_tree.column('#0', width=65, minwidth=65, stretch=False)
        self.purchase_tree.heading('#0', text='☐ 全选', command=self.toggle_all_purchases)
        self.purchase_tree.bind('<<TreeviewSelect>>', self.sync_purchase_checks)
        self.purchase_tree.bind('<Button-1>', self.toggle_purchase_check)
        self.purchase_tree.bind('<space>', self.toggle_focused_purchase)
        self.purchase_product.bind('<<ComboboxSelected>>', lambda _: self.refresh_purchase_history())

    def sync_purchase_checks(self, _=None):
        tree = self.purchase_tree
        selected = set(tree.selection())
        rows = tree.get_children()
        for row in rows:
            tree.item(row, text='☑' if row in selected else '☐')
        tree.heading('#0', text='☑ 全选' if rows and len(selected) == len(rows) else '☐ 全选')

    def toggle_all_purchases(self):
        tree = self.purchase_tree
        rows = tree.get_children()
        if rows and len(tree.selection()) == len(rows):
            tree.selection_remove(*rows)
        else:
            tree.selection_set(rows)
        self.sync_purchase_checks()

    def toggle_purchase_row(self, row):
        tree = self.purchase_tree
        if row in tree.selection():
            tree.selection_remove(row)
        else:
            tree.selection_add(row)
        tree.focus(row)
        self.sync_purchase_checks()

    def toggle_purchase_check(self, event):
        tree = self.purchase_tree
        if tree.identify_region(event.x, event.y) in ('tree', 'cell') and tree.identify_column(event.x) == '#0':
            row = tree.identify_row(event.y)
            if row:
                self.toggle_purchase_row(row)
                return 'break'

    def toggle_focused_purchase(self, _=None):
        row = self.purchase_tree.focus()
        if row:
            self.toggle_purchase_row(row)
        return 'break'

    def edit_purchase(self):
        selected = self.purchase_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning('请选择一条记录', '修改时请只选择一条入库记录')
            return
        item_id = int(selected[0])
        old = query('SELECT * FROM purchase_items WHERE id=?', (item_id,))[0]
        choices = dict(self.product_choices)
        label = next((label for label, pid in choices.items() if pid == old['product_id']), '')

        def save(values):
            product_id = choices.get(values['商品'])
            if product_id is None:
                raise ValueError('请选择商品')
            update_purchase(item_id, product_id, int(values['数量']), money(values['进价（元）']))
            self.refresh_all()

        self.edit_dialog('修改采购入库', [('商品', label, choices), ('数量', str(old['qty']), None),
                                       ('进价（元）', fmt(old['unit_price']), None)], save)

    def do_delete_purchase(self):
        selected = self.purchase_tree.selection()
        if not selected:
            messagebox.showwarning('未选择', '请先勾选要删除的入库记录')
            return
        details = []
        for row in selected:
            values = self.purchase_tree.item(row, 'values')
            details.append(f'{values[2]} {values[3]}：扣回 {values[4]}')
        if not messagebox.askyesno('删除采购入库', f'确定删除 {len(selected)} 条入库记录？库存将扣回：\n' + '\n'.join(details), parent=self):
            return
        try:
            delete_purchases([int(row) for row in selected])
            self.refresh_all()
        except (ValueError, sqlite3.Error) as ex:
            messagebox.showerror('删除失败', str(ex))

    def build_sale(self):
        f = self.frames['销售开单']
        first = ttk.Frame(f)
        first.pack(fill='x', pady=6)
        ttk.Label(first,text='客户').pack(side='left')
        self.sale_customer = SearchSelect(first, width=30)
        self.sale_customer.pack(side='left',padx=8)
        ttk.Button(first, text='新增客户', command=self.open_sale_customer).pack(side='left', padx=(0, 8))
        ttk.Label(first,text='备注').pack(side='left')
        self.sale_note = ttk.Entry(first,width=48)
        self.sale_note.pack(side='left',padx=8)
        second = ttk.Frame(f)
        second.pack(fill='x',pady=9)
        ttk.Label(second,text='商品').pack(side='left')
        self.sale_product = SearchSelect(second, width=36)
        self.sale_product.pack(side='left',padx=5)
        ttk.Label(second,text='数量').pack(side='left')
        self.sale_qty = ttk.Entry(second,width=8)
        self.sale_qty.pack(side='left',padx=5)
        ttk.Label(second,text='单价').pack(side='left')
        self.sale_price = ttk.Entry(second,width=10)
        self.sale_price.pack(side='left',padx=5)
        ttk.Button(second,text='加入清单',command=self.add_cart).pack(side='left',padx=5)
        ttk.Button(second,text='移除选中',command=self.remove_cart).pack(side='left')
        ttk.Button(second,text='修改选中',command=self.edit_cart).pack(side='left', padx=5)
        self.sale_product.bind('<<ComboboxSelected>>',self.fill_sale_price)
        self.sale_product.bind('<<SearchEdited>>', lambda _: self.sale_price.delete(0, 'end'))
        ttk.Label(f, text='点击输入框显示全部选项，可滚动查看；输入名称、电话或货号筛选，用鼠标或方向键、回车选中。',
                  foreground='#666666').pack(anchor='w')
        self.cart_tree = self.tree(f,['id','sku','name','qty','price','total'],
            [('id','商品ID',75),('sku','货号',110),('name','商品名称',290),('qty','数量',80),('price','单价',90),('total','小计',100)],height=12)
        self.cart_tree.bind('<Double-1>', lambda _: self.edit_cart())
        bottom = ttk.Frame(f)
        bottom.pack(fill='x',pady=8)
        self.total_label = ttk.Label(bottom,text='合计：¥0.00',font=('Microsoft YaHei UI',14,'bold'))
        self.total_label.pack(side='left',padx=10)
        ttk.Label(bottom,text='本次收款（元）').pack(side='left',padx=(24,5))
        self.paid = ttk.Entry(bottom,width=12)
        self.paid.insert(0,'0')
        self.paid.pack(side='left')
        ttk.Button(bottom,text='确认销售出库',command=self.do_sale).pack(side='right',padx=8)

    def create_sale_customer(self, name, phone='', address=''):
        customer_id = create_customer(name, phone, address)
        self.refresh_all()
        for label, cid in self.customer_choices.items():
            if cid == customer_id:
                self.sale_customer.set(label)
                break
        return customer_id

    def open_sale_customer(self):
        dialog = tk.Toplevel(self)
        dialog.title('新增客户')
        dialog.transient(self)
        dialog.resizable(False, False)
        content = ttk.Frame(dialog, padding=16)
        content.pack(fill='both', expand=True)
        entries = {}
        for row, label in enumerate(('客户名称', '电话', '地址')):
            ttk.Label(content, text=label + ('（必填）' if row == 0 else '')).grid(row=row, column=0, sticky='w', pady=6)
            entry = ttk.Entry(content, width=36)
            entry.grid(row=row, column=1, padx=8, pady=6)
            entries[label] = entry

        def save(_=None):
            try:
                self.create_sale_customer(entries['客户名称'].get(), entries['电话'].get(), entries['地址'].get())
            except (ValueError, sqlite3.Error) as ex:
                messagebox.showerror('新增失败', str(ex), parent=dialog)
                return
            dialog.destroy()

        buttons = ttk.Frame(content)
        buttons.grid(row=3, column=0, columnspan=2, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='保存并选中', command=save).pack(side='left', padx=5)
        ttk.Button(buttons, text='取消', command=dialog.destroy).pack(side='left')
        dialog.bind('<Return>', save)
        dialog.bind('<Escape>', lambda _: dialog.destroy())
        dialog.grab_set()
        entries['客户名称'].focus_set()

    def fill_sale_price(self, _=None):
        pid = self.product_choices.get(self.sale_product.get())
        if pid:
            p = query('SELECT sale_price FROM products WHERE id=?',(pid,))[0]
            self.sale_price.delete(0,'end')
            self.sale_price.insert(0,fmt(p['sale_price']))
            if _ is not None:
                self.sale_qty.delete(0, 'end')
                self.sale_qty.insert(0, '1')

    def add_cart(self):
        try:
            pid = self.product_choices.get(self.sale_product.get())
            if not pid:
                raise ValueError('请选择商品')
            quantity_text = self.sale_qty.get().strip()
            if not quantity_text:
                messagebox.showwarning('请填写数量', '请输入商品数量后再添加')
                self.sale_qty.focus_set()
                return
            try:
                qty = int(quantity_text)
            except ValueError:
                messagebox.showwarning('数量格式不正确', '商品数量请输入大于0的整数，例如：1、2、3')
                self.sale_qty.focus_set()
                return
            price = money(self.sale_price.get())
            if qty <= 0:
                raise ValueError('数量必须大于0')
            product = query('SELECT * FROM products WHERE id=?',(pid,))[0]
            qty_in_cart = sum(x[1] for x in self.cart if x[0] == pid)
            if qty + qty_in_cart > product['stock']:
                raise ValueError(f"库存不足：当前库存{product['stock']}，已加入{qty_in_cart}")
            self.cart.append((pid,qty,price))
            self.redraw_cart()
            self.sale_qty.delete(0,'end')
        except ValueError as ex:
            messagebox.showerror('添加失败',str(ex))

    def update_cart_line(self, index, product_id, qty, price):
        if qty <= 0 or price < 0:
            raise ValueError('数量必须大于0，单价不能为负数')
        products = query('SELECT stock FROM products WHERE id=? AND active=1', (product_id,))
        if not products:
            raise ValueError('商品不存在或已删除')
        other_qty = sum(line[1] for i, line in enumerate(self.cart)
                        if i != index and line[0] == product_id)
        if qty + other_qty > products[0]['stock']:
            raise ValueError(f"库存不足：当前库存{products[0]['stock']}，其他行已加入{other_qty}")
        self.cart[index] = (product_id, qty, price)
        self.redraw_cart()

    def edit_cart(self):
        selected = self.cart_tree.selection()
        if not selected:
            messagebox.showwarning('未选择', '请先选择要修改的清单行')
            return
        index = int(selected[0])
        product_id, qty, price = self.cart[index]
        choices = dict(self.product_choices)
        current = next((label for label, pid in choices.items() if pid == product_id), '')

        def save(values):
            pid = choices.get(values['商品'])
            if pid is None:
                raise ValueError('请选择商品')
            self.update_cart_line(index, pid, int(values['数量']), money(values['单价（元）']))

        self.edit_dialog('修改开单清单', [('商品', current, list(choices)),
                                        ('数量', str(qty), None), ('单价（元）', fmt(price), None)], save)

    def redraw_cart(self):
        self.cart_tree.delete(*self.cart_tree.get_children())
        total = 0
        for i,(pid,qty,price) in enumerate(self.cart):
            p = query('SELECT sku,name FROM products WHERE id=?',(pid,))[0]
            amount = qty * price
            total += amount
            self.cart_tree.insert('','end',iid=str(i),values=(pid,p['sku'],p['name'],qty,fmt(price),fmt(amount)))
        self.total_label.config(text=f'合计：¥{fmt(total)}')

    def remove_cart(self):
        selected = self.cart_tree.selection()
        if selected:
            del self.cart[int(selected[0])]
            self.redraw_cart()

    def do_sale(self):
        try:
            cid = self.customer_choices.get(self.sale_customer.get())
            if not cid:
                raise ValueError('请先在客户管理中添加并选择客户')
            paid = money(self.paid.get() or '0')
            sale_id = create_sale(cid,self.cart,paid,self.sale_note.get().strip())
            self.cart = []
            self.redraw_cart()
            self.sale_note.delete(0,'end')
            self.paid.delete(0,'end')
            self.paid.insert(0,'0')
            self.refresh_all()
            if messagebox.askyesno('开单成功',f'销售单 #{sale_id} 已保存，库存已扣减。\n是否打开打印预览？'):
                self.print_sale(sale_id)
        except ValueError as ex:
            messagebox.showerror('销售失败',str(ex))

    def build_debts(self):
        f = self.frames['账款管理']
        self.page_toolbar(f, refresh=True)
        self.only_due = tk.BooleanVar(value=False)
        self.debt_search = self.sales_search(f, show_due_filter=True)
        self.debt_tree = self.tree(f,['id','number','customer','date','total','paid','due','account_actions'],
            [('id','单据ID',70),('number','销售单号',205),('customer','客户',160),('date','日期',155),('total','应收',95),('paid','已收',95),('due','未收',95),('account_actions','操作',180)])
        self.row_buttons(self.debt_tree, 'account_actions', [
            ('明细', lambda row: self.show_sale_detail(int(row))),
            ('登记收款', self.receive_for_row)],
            action_filter=lambda row, label: label == '明细' or money(self.debt_tree.item(row, 'values')[6]) > 0)

    def receive_for_row(self, row):
        self.debt_tree.selection_set(row)
        self.do_receive()

    def do_receive(self):
        selected = self.debt_tree.selection()
        if not selected:
            messagebox.showwarning('未选择','请先选择一笔欠款')
            return
        sale_id = int(self.debt_tree.item(selected[0],'values')[0])
        value = simpledialog.askstring('登记收款','请输入本次收款金额（元）：',parent=self)
        if value is None:
            return
        try:
            receive_payment(sale_id,money(value))
            self.refresh_all()
            messagebox.showinfo('收款成功','收款已记录，客户欠款已更新')
        except ValueError as ex:
            messagebox.showerror('收款失败',str(ex))

    def build_history(self):
        f = self.frames['销售记录']
        self.page_toolbar(f, [('打印选中单据', self.print_selected), ('导出销售CSV', self.export_sales)], refresh=True)
        self.history_search = self.sales_search(f)
        self.history_tree = self.tree(f,['id','number','customer','date','total','paid','due','detail'],
            [('id','单据ID',70),('number','销售单号',205),('customer','客户',155),('date','日期',155),('total','应收',95),('paid','已收',95),('due','欠款',95),('detail','操作',80)])
        self.row_buttons(self.history_tree, 'detail', [('明细', lambda row: self.show_sale_detail(int(row)))])

    def sale_rows(self, where=''):
        return query('''SELECT s.id,s.number,c.name customer,c.phone,s.created_at,s.total,
                      COALESCE(SUM(p.amount),0) paid,s.total-COALESCE(SUM(p.amount),0) due
                      FROM sales s JOIN customers c ON c.id=s.customer_id
                      LEFT JOIN payments p ON p.sale_id=s.id
                      GROUP BY s.id ''' + where + ' ORDER BY s.id DESC')

    def refresh_all(self):
        selected_customer = self.customer_choices.get(self.sale_customer.get())
        products = query('SELECT * FROM products WHERE active=1 ORDER BY id DESC')
        checked = self.checked_product_ids.intersection(str(p['id']) for p in products)
        self.product_tree.delete(*self.product_tree.get_children())
        for p in products:
            if self.product_search.get().strip().casefold() not in (p['sku'] + ' ' + p['name']).casefold():
                continue
            tag = ('low',) if p['stock'] <= p['min_stock'] else ()
            self.product_tree.insert('','end',iid=str(p['id']),text='☐',values=(p['id'],p['sku'],p['name'],p['unit'],fmt(p['purchase_price']),fmt(p['sale_price']),p['stock'],p['min_stock'],''),tags=tag)
        self.product_tree.selection_set([row for row in checked if self.product_tree.exists(row)])
        self.sync_product_checks()
        self.product_tree.tag_configure('low',background='#ffe6e6')
        self.product_choices = {f"{p['sku']} | {p['name']} (库存{p['stock']})":p['id'] for p in products}
        product_search = {label: f"{p['sku']} {p['name']}" for label, p in zip(self.product_choices, products)}
        self.purchase_product.set_options(self.product_choices, product_search)
        self.sale_product.set_options(self.product_choices, product_search)
        for selector in (self.purchase_product, self.sale_product):
            if selector.get() not in self.product_choices:
                selector.set('')
        customers = query('SELECT * FROM customers ORDER BY id DESC')
        self.customer_tree.delete(*self.customer_tree.get_children())
        for c in customers:
            text = f"{c['name']} {c['phone']} {c['address']}".casefold()
            if not all(word in text for word in self.customer_search.get().casefold().split()):
                continue
            self.customer_tree.insert('','end',iid=str(c['id']),values=(c['id'],c['name'],c['phone'],c['address'],''))
        self.customer_choices = {f"{c['id']} | {c['name']}" + (f"（{c['phone']}）" if c['phone'] else ''):c['id'] for c in customers}
        self.sale_customer.set_options(self.customer_choices, {
            label: f"{c['id']} {c['name']} {c['phone']}" for label, c in zip(self.customer_choices, customers)})
        self.sale_customer.set(next((label for label, cid in self.customer_choices.items()
                                     if cid == selected_customer), ''))
        self.refresh_purchase_history()
        self.refresh_sales()

    def refresh_sales(self):
        rows = self.sale_rows()
        self.history_tree.delete(*self.history_tree.get_children())
        self.debt_tree.delete(*self.debt_tree.get_children())
        for r in rows:
            vals = (r['id'],r['number'],r['customer'],r['created_at'],fmt(r['total']),fmt(r['paid']),fmt(r['due']))
            text = f"{r['customer']} {r['phone']} {r['number']} {r['created_at']}".casefold()
            if all(word in text for word in self.history_search.get().casefold().split()):
                self.history_tree.insert('','end',iid=str(r['id']),values=vals+('',))
            if (not self.only_due.get() or r['due'] > 0) and all(word in text for word in self.debt_search.get().casefold().split()):
                self.debt_tree.insert('','end',iid=str(r['id']),values=vals+('',))

    def show_sale_detail(self, sale_id):
        sale = query('SELECT s.*,c.name customer,c.phone,c.address FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.id=?', (sale_id,))[0]
        items = query('SELECT * FROM sale_items WHERE sale_id=? ORDER BY id', (sale_id,))
        payments = query('SELECT * FROM payments WHERE sale_id=? ORDER BY created_at,id', (sale_id,))
        paid = sum(row['amount'] for row in payments)
        dialog = tk.Toplevel(self)
        dialog.title('销售明细 · ' + sale['number'])
        dialog.geometry('950x650')
        dialog.transient(self)
        content = ttk.Frame(dialog, padding=14)
        content.pack(fill='both', expand=True)
        info = f"单号：{sale['number']}    日期：{sale['created_at']}\n客户：{sale['customer']}    电话：{sale['phone']}\n客户地址：{sale['address']}\n备注：{sale['note']}"
        ttk.Label(content, text=info, wraplength=880, justify='left').pack(anchor='w')
        ttk.Label(content, text='销售清单').pack(anchor='w', pady=(10,0))
        listing = self.tree(content, ['sku','name','unit','qty','price','amount'],
                            [('sku','货号',120),('name','商品名称',250),('unit','单位',60),('qty','数量',80),('price','单价',100),('amount','金额',110)], height=6)
        for item in items:
            listing.insert('', 'end', values=(item['sku'],item['product_name'],item['unit'],item['qty'],fmt(item['unit_price']),fmt(item['amount'])))
        ttk.Label(content, text=f"应收：¥{fmt(sale['total'])}    已收：¥{fmt(paid)}    未收：¥{fmt(sale['total']-paid)}").pack(anchor='w', pady=6)
        ttk.Label(content, text='收款流水' if payments else '收款流水：暂无收款记录').pack(anchor='w')
        payment_tree = self.tree(content, ['date','amount','note'], [('date','收款时间',200),('amount','金额',120),('note','备注',400)], height=4)
        for payment in payments:
            payment_tree.insert('', 'end', values=(payment['created_at'],fmt(payment['amount']),payment['note']))
        ttk.Button(content, text='关闭', command=dialog.destroy).pack(anchor='e')
        return dialog

    def clear_purchase_filter(self):
        self.purchase_product.set('')
        self.refresh_purchase_history()

    def refresh_purchase_history(self):
        product_id = self.product_choices.get(self.purchase_product.get())
        purchases = query('''SELECT pi.id,pu.number,pu.created_at,pr.sku,pr.name,pi.qty,pi.unit_price,pi.amount
                             FROM purchases pu JOIN purchase_items pi ON pi.purchase_id=pu.id
                             JOIN products pr ON pr.id=pi.product_id WHERE (? IS NULL OR pi.product_id=?) ORDER BY pu.id DESC''', (product_id, product_id))
        self.purchase_tree.delete(*self.purchase_tree.get_children())
        for p in purchases:
            self.purchase_tree.insert('','end',iid=str(p[0]),text='☐',values=(p[1],p[2],p[3],p[4],p[5],fmt(p[6]),fmt(p[7])))
        self.sync_purchase_checks()

    def print_selected(self):
        selected = self.history_tree.selection()
        if selected:
            sale_id = int(self.history_tree.item(selected[0],'values')[0])
            self.print_sale(sale_id)
        else:
            messagebox.showwarning('未选择','请选择一笔销售记录')

    def print_sale(self,sale_id):
        company = load_settings()
        sale = query('''SELECT s.*,c.name customer,c.phone,c.address FROM sales s
                        JOIN customers c ON c.id=s.customer_id WHERE s.id=?''',(sale_id,))[0]
        items = query('SELECT * FROM sale_items WHERE sale_id=? ORDER BY id',(sale_id,))
        paid = query('SELECT COALESCE(SUM(amount),0) v FROM payments WHERE sale_id=?',(sale_id,))[0]['v']
        safe = lambda x: html.escape(str(x or ''))
        rows = ''.join(f"<tr><td>{i}</td><td class='text'>{safe(x['sku'])}</td><td class='text'>{safe(x['product_name'])}</td><td>{safe(x['unit'])}</td><td>{x['qty']}</td><td class='number'>{fmt(x['unit_price'])}</td><td class='number'>{fmt(x['amount'])}</td><td></td><td></td></tr>" for i,x in enumerate(items,1))
        page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>批发清单 {safe(sale['number'])}</title>
<style>
@page{{size:A4 landscape;margin:12mm}}
*{{box-sizing:border-box}}body{{margin:0;color:#000;background:#fff;font-family:"黑体","SimHei","Heiti SC",sans-serif;font-size:{company['body_font']}pt;font-weight:bold}}
.sheet{{max-width:273mm;margin:0 auto;padding-top:3mm}}
h1{{text-align:center;font-size:{company['company_name_font']}pt;line-height:1.35;margin:0 0 2mm;overflow-wrap:anywhere}}
.scope{{text-align:center;font-size:{company['business_scope_font']}pt;line-height:1.4;margin-bottom:3mm;white-space:pre-wrap;overflow-wrap:anywhere}}
.meta{{display:grid;grid-template-columns:22% 40% 38%;line-height:1.65;margin-bottom:7mm;overflow-wrap:anywhere}}
.customer{{grid-column:span 2}}.order{{font-size:inherit}}
table{{font-size:inherit;width:100%;border-collapse:collapse;table-layout:auto}}
th,td{{border:1px solid #000;height:8mm;padding:1mm;text-align:center;font-weight:inherit;overflow-wrap:anywhere}}
th{{font-size:inherit}}td.text{{text-align:left}}thead{{display:table-header-group}}tr{{break-inside:avoid}}
td.number,th.number{{white-space:nowrap;overflow-wrap:normal;word-break:normal;width:1%}}
.summary td{{height:9mm}}.summary .text{{font-size:inherit}}
.footer{{display:grid;grid-template-columns:62% 38%;gap:3mm 0;line-height:1.6;margin-top:6mm;break-inside:avoid;overflow-wrap:anywhere}}
.company-address{{font-size:{company['company_address_font']}pt}}
.company-phone{{font-size:{company['company_phone_font']}pt}}
.settlement{{grid-column:span 2;font-size:inherit}}.note{{grid-column:span 2;white-space:pre-wrap;font-size:inherit}}
.signature{{grid-column:2;margin-top:3mm;font-size:inherit;min-height:12mm}}.signature span{{display:inline-block;width:48mm;border-bottom:1px solid #000}}
.hint{{font-family:inherit;font-size:10pt;color:#666;margin:8mm 0}}
@media screen{{body{{padding:16px}}.sheet{{min-width:900px}}}}
@media print{{.hint{{display:none}}}}
</style></head><body><main class="sheet">
<h1>{safe(company['company_name']) or '汽配'}批发清单</h1>
<div class="scope">{safe(company['business_scope'])}</div>
<div class="meta"><div class="customer">客户名称：{safe(sale['customer'])}</div><div>销售日期：{safe(sale['created_at'][:10])}</div>
<div>电话：{safe(sale['phone'])}</div><div>客户地址：{safe(sale['address'])}</div><div class="order">单号：{safe(sale['number'])}</div></div>
<table><colgroup><col style="width:7%"><col style="width:12%"><col style="width:21%"><col style="width:7%"><col style="width:7%"><col><col><col style="width:14%"><col style="width:12%"></colgroup>
<thead><tr><th>序号</th><th>配件编码</th><th>名称</th><th>单位</th><th>数量</th><th class='number'>单价</th><th class='number'>金额</th><th>品牌</th><th>备注</th></tr></thead>
<tbody>{rows}</tbody><tbody class="summary"><tr><td colspan="5" class="text">人民币（大写）：{money_upper(sale['total'])}</td><td>------</td><td class='number'>{fmt(sale['total'])}</td><td>----------</td><td>------</td></tr></tbody></table>
<div class="footer"><div class="company-address">地址：{safe(company['company_address'])}</div><div class="company-phone">电话：{safe(company['company_phone'])}</div>
<div class="settlement">已收：¥{fmt(paid)}　未收：¥{fmt(sale['total']-paid)}</div>
{('<div class="note">备注：' + safe(sale['note']) + '</div>') if sale['note'] else ''}
<div class="signature">客户签名：<span></span></div></div>
<div class="hint">按 Ctrl+P 打印，纸张选择 A4、横向，并关闭浏览器页眉和页脚。</div>
</main></body></html>'''
        path = PRINT_DIR / f"{sale['number']}.html"
        path.write_text(page,encoding='utf-8')
        webbrowser.open(path.as_uri())

    def export_sales(self):
        path = filedialog.asksaveasfilename(title='导出销售记录',defaultextension='.csv',filetypes=[('CSV文件','*.csv')],initialfile='销售记录.csv')
        if not path:
            return
        with open(path,'w',encoding='utf-8-sig',newline='') as f:
            w = csv.writer(f)
            w.writerow(['单据ID','销售单号','客户','日期','应收','已收','欠款'])
            for r in self.sale_rows():
                w.writerow([r['id'],r['number'],r['customer'],r['created_at'],fmt(r['total']),fmt(r['paid']),fmt(r['due'])])
        messagebox.showinfo('导出成功',f'已导出至：{path}')

    def backup(self):
        target = filedialog.asksaveasfilename(title='备份数据库',defaultextension='.db',filetypes=[('SQLite数据库','*.db')],initialfile=f"汽配备份-{datetime.now():%Y%m%d-%H%M%S}.db")
        if not target:
            return
        try:
            with db() as source, closing(sqlite3.connect(target)) as dest:
                source.backup(dest)
            messagebox.showinfo('备份成功',f'数据库已备份至：{target}')
        except (OSError,sqlite3.Error) as ex:
            messagebox.showerror('备份失败',str(ex))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        # 构建时验证冻结程序的 Tk、资源和数据库，全部使用临时数据。
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as smoke_dir:
            DB_PATH = Path(smoke_dir) / 'autoparts.db'
            PRINT_DIR = Path(smoke_dir) / 'printouts'
            PRINT_DIR.mkdir()
            init_db()
            window = App()
            # 在 Windows runner 上实际验证输入、候选选择与价格带出。
            create_customer('搜索测试客户', '13800138000')
            create_product('搜索测试配件', price=2300)
            for i in range(12):
                create_customer(f'其他客户{i}')
                create_product(f'其他配件{i}')
            window.refresh_all()
            window.update()
            for selector, keyword in ((window.sale_customer, '1380013'),
                                      (window.sale_product, '测试配件')):
                selector.entry.focus_force()
                selector.entry.event_generate('<Button-1>', x=5, y=5)
                window.update()
                assert selector.matches == selector.options
                assert selector.results.size() == 13
                assert int(selector.results['height']) == 10
                assert selector.popup.winfo_viewable()
                assert selector.popup.winfo_width() == selector.entry.winfo_width()
                selector.entry.insert(0, keyword)
                window.update()
                selector.show_results()
                assert len(selector.matches) == 1
                selector.accept()
                window.update()
                assert selector.get() in selector.options
            assert window.sale_price.get() == '23.00'
            assert window.sale_qty.get() == '1'
            dialog = window.open_product_purchase(1)
            window.update()
            assert '采购入库' in dialog.title()
            entries = [w for w in dialog.winfo_children()[0].winfo_children() if isinstance(w, ttk.Entry)]
            entries[0].insert(0, '3')
            entries[1].delete(0, 'end')
            entries[1].insert(0, '12.00')
            buttons = [w for w in dialog.winfo_children()[0].winfo_children() if isinstance(w, ttk.Frame)][0]
            next(w for w in buttons.winfo_children() if w.cget('text') == '保存').invoke()
            assert query('SELECT stock FROM products WHERE id=1')[0][0] == 3
            window.open_purchase_history()
            window.update()
            assert window.purchase_window.winfo_viewable()
            window.purchase_window.withdraw()
            window.tabs.select(window.frames['商品库存'])
            window.update()
            dialog = window.add_product()
            window.update()
            unit = next(w for w in dialog.winfo_children()[0].winfo_children() if isinstance(w, SearchSelect))
            unit.entry.focus_force()
            unit.entry.event_generate('<Button-1>', x=5, y=5)
            window.update()
            assert unit.matches == ['个', '件']
            unit.accept()
            assert unit.get() == '个'
            dialog.destroy()
            dialog = window.edit_dialog('下拉框检查', [('商品', '', list(window.product_choices))], lambda _: None)
            window.update()
            selector = next(control for control in dialog.winfo_children()[0].winfo_children()
                            if isinstance(control, SearchSelect))
            selector.entry.focus_force()
            selector.entry.event_generate('<Button-1>', x=5, y=5)
            window.update()
            assert selector.results.size() == 13
            assert selector.popup.winfo_viewable()
            selector.entry.insert(0, '测试配件')
            selector.show_results()
            assert len(selector.matches) == 1
            selector.accept()
            assert '测试配件' in selector.get()
            dialog.destroy()
            window.update()
            create_purchase(1, 10, 100)
            window.refresh_all()
            window.toggle_all_purchases()
            assert len(window.purchase_tree.selection()) == 2
            window.purchase_tree.selection_set(window.purchase_tree.get_children()[0])
            window.edit_purchase()
            window.update()
            editor = next(w for w in window.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == '修改采购入库')
            editor.destroy()
            window.product_tree.selection_set('1')
            window.edit_product()
            window.update()
            editor = next(w for w in window.winfo_children() if isinstance(w, tk.Toplevel) and w.title().startswith('修改商品'))
            editor.destroy()
            sale_id = create_sale(1, [(1, 2, 2300)], paid=1000)
            window.refresh_all()
            window.history_search.set('搜索测试客户')
            window.debt_search.set('13800138000')
            assert len(window.history_tree.get_children()) == 1
            assert len(window.debt_tree.get_children()) == 1
            window.history_search.set('不存在的单号')
            assert not window.history_tree.get_children()
            window.history_search.set('')
            detail = window.show_sale_detail(sale_id)
            window.update()
            assert detail.winfo_viewable()
            detail.destroy()
            window.tabs.select(window.frames['商品库存'])
            for geometry in ('1200x800', '1000x700'):
                window.geometry(geometry)
                window.update()
                window.after(250, window.quit)
                window.mainloop()
                window.update_idletasks()
                assert window.product_tree.action_frames
                for frame in window.product_tree.action_frames.values():
                    for button in frame.winfo_children():
                        assert button.winfo_height() >= button.winfo_reqheight()
                        assert button.winfo_width() >= button.winfo_reqwidth()
                        assert button.winfo_x() + button.winfo_width() <= frame.winfo_width()
            customer_dialog = window.add_customer()
            window.update()
            fields = [w for w in customer_dialog.winfo_children()[0].winfo_children() if isinstance(w, ttk.Entry)]
            fields[0].insert(0, '弹窗新增验证')
            buttons = next(w for w in customer_dialog.winfo_children()[0].winfo_children() if isinstance(w, ttk.Frame))
            next(w for w in buttons.winfo_children() if w.cget('text') == '保存').invoke()
            added = query('SELECT id FROM customers WHERE name=?', ('弹窗新增验证',))[0][0]
            assert window.customer_tree.exists(str(added))
            window.customer_action(str(added), window.edit_customer)
            window.update()
            next(w for w in window.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == '修改客户').destroy()
            delete_customer(added)
            window.refresh_all()
            assert not window.customer_tree.exists(str(added))
            window.customer_search.set('13800138000')
            assert len(window.customer_tree.get_children()) == 1
            window.customer_search.set('无匹配客户')
            assert not window.customer_tree.get_children()
            window.customer_search.set('')
            window.tabs.select(window.frames['账款管理'])
            window.update()
            window.after(250, window.quit)
            window.mainloop()
            assert window.debt_tree.action_frames[str(sale_id)].action_labels == ('明细', '登记收款')
            receive_payment(sale_id, 3600)
            window.refresh_all()
            window.update()
            window.after(250, window.quit)
            window.mainloop()
            assert window.debt_tree.exists(str(sale_id))
            assert window.debt_tree.action_frames[str(sale_id)].action_labels == ('明细',)
            window.only_due.set(True)
            window.refresh_sales()
            assert not window.debt_tree.get_children()
            assert window.title() == APP_NAME
            assert len(window.tabs.tabs()) == 6
            assert hasattr(window, 'app_icon')
            window.destroy()
            Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'name': APP_NAME}, ensure_ascii=False), encoding='utf-8')
    else:
        init_db()
        App().mainloop()
