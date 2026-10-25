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
    values.update({r['key']: r['value'] for r in query('SELECT key,value FROM settings')
                   if r['key'] in COMPANY_FIELDS})
    return values


def save_settings(values):
    with db() as c:
        c.executemany('INSERT INTO settings(key,value) VALUES(?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      [(key, values.get(key, '').strip()) for key in COMPANY_FIELDS])


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
        for name in ['销售开单', '商品库存', '采购入库', '客户管理', '客户欠款', '销售记录', '系统设置']:
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
        tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(frame, command=tree.yview)
        sb.pack(side='right', fill='y')
        tree.configure(yscrollcommand=sb.set)
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

    def build_products(self):
        f = self.frames['商品库存']
        ttk.Label(f, text='货号在保存时自动生成，例如 SP000001。').pack(anchor='w', pady=5)
        line, self.p_entries = self.formline(f, [('商品名称', 24)])
        ttk.Label(line, text='单位').pack(side='left', padx=4)
        self.product_unit = SearchSelect(line, width=5)
        self.product_unit.set_options(('个', '件'))
        self.product_unit.set('件')
        self.product_unit.pack(side='left', padx=(0, 10))
        for label, width in [('售价', 10), ('进价', 10), ('预警库存', 9)]:
            ttk.Label(line, text=label).pack(side='left', padx=4)
            entry = ttk.Entry(line, width=width)
            entry.pack(side='left', padx=(0, 10))
            self.p_entries[label] = entry
        ttk.Button(line, text='新增', command=self.add_product).pack(side='left')
        ttk.Button(line, text='刷新', command=self.refresh_all).pack(side='left', padx=5)
        actions = ttk.Frame(f)
        actions.pack(fill='x', pady=4)
        ttk.Button(actions, text='删除', command=self.do_delete_product).pack(side='left')
        ttk.Button(actions, text='修改销售价', command=self.edit_product_price).pack(side='left', padx=5)
        ttk.Label(actions, text='仅可删除零库存商品，历史单据和流水保留。').pack(side='left', padx=10)
        self.product_tree = self.tree(f, ['id','sku','name','unit','cost','price','stock','minstock'],
            [('id','ID',45),('sku','货号',100),('name','商品名称',250),('unit','单位',70),('cost','参考进价',90),('price','销售价',90),('stock','库存',80),('minstock','预警值',80)])
        self.checked_product_ids = set()
        self.product_tree.configure(show='tree headings', selectmode='extended')
        self.product_tree.column('#0', width=65, minwidth=65, stretch=False)
        self.product_tree.heading('#0', text='☐ 全选', command=self.toggle_all_products)
        self.product_tree.bind('<Button-1>', self.toggle_product_check)
        self.product_tree.bind('<<TreeviewSelect>>', self.sync_product_checks)
        self.product_tree.bind('<space>', self.toggle_focused_product)

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

    def edit_product_price(self):
        selected = self.product_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning('请选择一个商品', '修改销售价时请只选择一个商品')
            return
        product_id = int(self.product_tree.item(selected[0], 'values')[0])
        product = query('SELECT * FROM products WHERE id=?', (product_id,))[0]

        def save(values):
            update_product_price(product_id, money(values['销售价（元）']))
            self.refresh_all()
            if self.product_choices.get(self.sale_product.get()) == product_id:
                self.fill_sale_price()

        self.edit_dialog(f"修改销售价 · {product['name']}",
                         [('销售价（元）', fmt(product['sale_price']), None)], save)

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
        try:
            e = self.p_entries
            name = e['商品名称'].get().strip()
            unit = self.product_unit.get()
            price = money(e['售价'].get() or '0')
            cost = money(e['进价'].get() or '0')
            minimum = int(e['预警库存'].get() or '0')
            if minimum < 0:
                raise ValueError('预警库存不能为负数')
            create_product(name, unit, price, cost, minimum)
            for item in e.values():
                item.delete(0,'end')
            self.refresh_all()
        except (ValueError, sqlite3.IntegrityError) as ex:
            messagebox.showerror('新增失败', str(ex))

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
        for key, label in COMPANY_FIELDS.items():
            _, entries = self.formline(f, [(label, 65)])
            entries[label].insert(0, values[key])
            self.settings_entries[key] = entries[label]
        ttk.Button(f, text='保存设置', command=self.do_save_settings).pack(anchor='w', pady=12)

    def do_save_settings(self):
        try:
            save_settings({key: entry.get() for key, entry in self.settings_entries.items()})
            messagebox.showinfo('保存成功', '公司信息已保存，将用于后续打印。')
        except sqlite3.Error as ex:
            messagebox.showerror('保存失败', str(ex))

    def build_customers(self):
        f = self.frames['客户管理']
        line, self.c_entries = self.formline(f, [('客户名称', 23), ('电话', 20), ('地址', 38)])
        ttk.Button(line, text='新增客户', command=self.add_customer).pack(side='left')
        ttk.Button(line, text='修改', command=self.edit_customer).pack(side='left', padx=5)
        self.customer_tree = self.tree(f, ['id','name','phone','address'],
            [('id','ID',60),('name','客户名称',210),('phone','联系电话',170),('address','客户地址',380)])
        self.customer_tree.bind('<Double-1>', lambda _: self.edit_customer())

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
        try:
            e = self.c_entries
            create_customer(e['客户名称'].get(), e['电话'].get(), e['地址'].get())
            for item in e.values():
                item.delete(0,'end')
            self.refresh_all()
        except (ValueError, sqlite3.IntegrityError) as ex:
            messagebox.showerror('新增失败', str(ex))

    def build_purchase(self):
        f = self.frames['采购入库']
        ttk.Label(f, text='第一版每次录入一种商品；多种商品可连续操作。', foreground='#555').pack(anchor='w', pady=7)
        line = ttk.Frame(f)
        line.pack(fill='x', pady=10)
        ttk.Label(line,text='商品').pack(side='left')
        self.purchase_product = SearchSelect(line, width=42)
        self.purchase_product.pack(side='left', padx=6)
        ttk.Label(line,text='数量').pack(side='left')
        self.purchase_qty = ttk.Entry(line, width=9)
        self.purchase_qty.pack(side='left', padx=6)
        ttk.Label(line,text='进价（元）').pack(side='left')
        self.purchase_price = ttk.Entry(line, width=11)
        self.purchase_price.pack(side='left', padx=6)
        ttk.Button(line,text='确认入库',command=self.do_purchase).pack(side='left', padx=7)
        self.purchase_tree = self.tree(f, ['number','time','name','qty','price','total'],
            [('number','采购单号',205),('time','时间',150),('name','商品',220),('qty','数量',80),('price','单价',90),('total','金额',100)])
        self.purchase_product.bind('<<ComboboxSelected>>', self.fill_purchase_price)
        self.purchase_product.bind('<<SearchEdited>>', lambda _: self.purchase_price.delete(0, 'end'))

    def fill_purchase_price(self, _=None):
        pid = self.product_choices.get(self.purchase_product.get())
        if pid:
            p = query('SELECT purchase_price FROM products WHERE id=?', (pid,))[0]
            self.purchase_price.delete(0, 'end')
            self.purchase_price.insert(0, fmt(p['purchase_price']))

    def do_purchase(self):
        try:
            pid = self.product_choices.get(self.purchase_product.get())
            if not pid:
                raise ValueError('请选择商品')
            qty = int(self.purchase_qty.get())
            price = money(self.purchase_price.get())
            purchase_id = create_purchase(pid, qty, price)
            self.purchase_qty.delete(0,'end')
            messagebox.showinfo('采购成功', f'采购入库已保存，编号：{purchase_id}')
            self.refresh_all()
        except ValueError as ex:
            messagebox.showerror('入库失败',str(ex))

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

    def add_cart(self):
        try:
            pid = self.product_choices.get(self.sale_product.get())
            if not pid:
                raise ValueError('请选择商品')
            qty = int(self.sale_qty.get())
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
        f = self.frames['客户欠款']
        ttk.Label(f,text='显示尚未结清的销售单；选择一条后点击“登记收款”。').pack(anchor='w',pady=6)
        actions = ttk.Frame(f)
        actions.pack(fill='x')
        ttk.Button(actions,text='登记收款',command=self.do_receive).pack(side='left',padx=4)
        ttk.Button(actions,text='刷新',command=self.refresh_all).pack(side='left',padx=4)
        self.debt_tree = self.tree(f,['id','number','customer','date','total','paid','due'],
            [('id','单据ID',70),('number','销售单号',205),('customer','客户',160),('date','日期',155),('total','应收',95),('paid','已收',95),('due','未收',95)])

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
        actions = ttk.Frame(f)
        actions.pack(fill='x')
        ttk.Button(actions,text='打印选中单据',command=self.print_selected).pack(side='left',padx=5)
        ttk.Button(actions,text='导出销售CSV',command=self.export_sales).pack(side='left',padx=5)
        ttk.Button(actions,text='刷新',command=self.refresh_all).pack(side='left',padx=5)
        self.history_tree = self.tree(f,['id','number','customer','date','total','paid','due'],
            [('id','单据ID',70),('number','销售单号',205),('customer','客户',155),('date','日期',155),('total','应收',95),('paid','已收',95),('due','欠款',95)])

    def sale_rows(self, where=''):
        return query('''SELECT s.id,s.number,c.name customer,s.created_at,s.total,
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
            tag = ('low',) if p['stock'] <= p['min_stock'] else ()
            self.product_tree.insert('','end',iid=str(p['id']),text='☐',values=(p['id'],p['sku'],p['name'],p['unit'],fmt(p['purchase_price']),fmt(p['sale_price']),p['stock'],p['min_stock']),tags=tag)
        self.product_tree.selection_set(list(checked))
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
            self.customer_tree.insert('','end',values=(c['id'],c['name'],c['phone'],c['address']))
        self.customer_choices = {f"{c['id']} | {c['name']}" + (f"（{c['phone']}）" if c['phone'] else ''):c['id'] for c in customers}
        self.sale_customer.set_options(self.customer_choices, {
            label: f"{c['id']} {c['name']} {c['phone']}" for label, c in zip(self.customer_choices, customers)})
        self.sale_customer.set(next((label for label, cid in self.customer_choices.items()
                                     if cid == selected_customer), ''))
        purchases = query('''SELECT pu.number,pu.created_at,pr.name,pi.qty,pi.unit_price,pi.amount
                             FROM purchases pu JOIN purchase_items pi ON pi.purchase_id=pu.id
                             JOIN products pr ON pr.id=pi.product_id ORDER BY pu.id DESC LIMIT 500''')
        self.purchase_tree.delete(*self.purchase_tree.get_children())
        for p in purchases:
            self.purchase_tree.insert('','end',values=(p[0],p[1],p[2],p[3],fmt(p[4]),fmt(p[5])))
        rows = self.sale_rows()
        self.history_tree.delete(*self.history_tree.get_children())
        self.debt_tree.delete(*self.debt_tree.get_children())
        for r in rows:
            vals = (r['id'],r['number'],r['customer'],r['created_at'],fmt(r['total']),fmt(r['paid']),fmt(r['due']))
            self.history_tree.insert('','end',values=vals)
            if r['due'] > 0:
                self.debt_tree.insert('','end',values=vals)

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
        rows = ''.join(f"<tr><td>{i}</td><td class='text'>{safe(x['sku'])}</td><td class='text'>{safe(x['product_name'])}</td><td>{safe(x['unit'])}</td><td>{x['qty']}</td><td>{fmt(x['unit_price'])}</td><td>{fmt(x['amount'])}</td><td></td><td></td></tr>" for i,x in enumerate(items,1))
        page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>批发清单 {safe(sale['number'])}</title>
<style>
@page{{size:A4 landscape;margin:12mm}}
*{{box-sizing:border-box}}body{{margin:0;color:#000;background:#fff;font-family:"宋体","SimSun","Songti SC",serif;font-size:13pt;font-weight:bold}}
.sheet{{max-width:273mm;margin:0 auto;padding-top:3mm}}
h1{{text-align:center;font-size:25pt;line-height:1.35;margin:0 0 2mm;overflow-wrap:anywhere}}
.scope{{text-align:center;font-size:18pt;line-height:1.4;margin-bottom:3mm;white-space:pre-wrap;overflow-wrap:anywhere}}
.meta{{display:grid;grid-template-columns:22% 40% 38%;line-height:1.65;margin-bottom:7mm;overflow-wrap:anywhere}}
.customer{{grid-column:span 2}}.order{{font-size:inherit}}
table{{font-size:inherit;width:100%;border-collapse:collapse;table-layout:fixed}}
th,td{{border:1px solid #000;height:8mm;padding:1mm;text-align:center;font-weight:inherit;overflow-wrap:anywhere}}
th{{font-size:inherit}}td.text{{text-align:left}}thead{{display:table-header-group}}tr{{break-inside:avoid}}
.summary td{{height:9mm}}.summary .text{{font-size:inherit}}
.footer{{display:grid;grid-template-columns:62% 38%;gap:3mm 0;line-height:1.6;margin-top:6mm;break-inside:avoid;overflow-wrap:anywhere}}
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
<table><colgroup><col style="width:7%"><col style="width:12%"><col style="width:21%"><col style="width:7%"><col style="width:7%"><col style="width:8%"><col style="width:12%"><col style="width:14%"><col style="width:12%"></colgroup>
<thead><tr><th>序号</th><th>配件编码</th><th>名称</th><th>单位</th><th>数量</th><th>单价</th><th>金额</th><th>品牌</th><th>备注</th></tr></thead>
<tbody>{rows}</tbody><tbody class="summary"><tr><td colspan="5" class="text">人民币（大写）：{money_upper(sale['total'])}</td><td>------</td><td>{fmt(sale['total'])}</td><td>----------</td><td>------</td></tr></tbody></table>
<div class="footer"><div>地址：{safe(company['company_address'])}</div><div>电话：{safe(company['company_phone'])}</div>
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
            window.tabs.select(window.frames['采购入库'])
            window.update()
            selector = window.purchase_product
            selector.entry.focus_force()
            selector.entry.event_generate('<Button-1>', x=5, y=5)
            window.update()
            assert selector.results.size() == 13
            assert selector.popup.winfo_viewable()
            selector.entry.insert(0, '测试配件')
            selector.show_results()
            assert len(selector.matches) == 1
            selector.accept()
            assert window.purchase_price.get() == '0.00'
            window.tabs.select(window.frames['商品库存'])
            window.update()
            window.product_unit.entry.focus_force()
            window.product_unit.entry.event_generate('<Button-1>', x=5, y=5)
            window.update()
            assert window.product_unit.matches == ['个', '件']
            window.product_unit.accept()
            assert window.product_unit.get() == '个'
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
            assert window.title() == APP_NAME
            assert len(window.tabs.tabs()) == 7
            assert hasattr(window, 'app_icon')
            window.destroy()
            Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'name': APP_NAME}, ensure_ascii=False), encoding='utf-8')
    else:
        init_db()
        App().mainloop()
