"""打印内容回归测试；只使用临时数据库，不打开浏览器。"""
import importlib.util
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class PrintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location('print_test_app', Path(__file__).with_name('app.py'))
        self.app = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name}):
            spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.save_settings(dict(company_name='测试公司<&>', business_scope='马达 发电机',
                                    company_address='测试路8号', company_phone='12345678'))
        with self.app.db() as c:
            self.customer = c.execute("INSERT INTO customers(name,phone,address) VALUES(?,?,?)",
                                      ('测试客户', '87654321', '客户地址')).lastrowid
        self.app.create_product('测试配件', '个', 2300)
        self.product = self.app.query('SELECT id FROM products')[0]['id']
        self.app.create_purchase(self.product, 100, 1000)

    def print_order(self, lines):
        sale_id = self.app.create_sale(self.customer, lines, paid=100)
        with patch.object(self.app.webbrowser, 'open') as browser:
            self.app.App.print_sale(None, sale_id)
            browser.assert_called_once()
        number = self.app.query('SELECT number FROM sales WHERE id=?', (sale_id,))[0]['number']
        return (self.app.PRINT_DIR / f'{number}.html').read_text(encoding='utf-8')

    def test_reference_layout(self):
        page = self.print_order([(self.product, 6, 2300)])
        for text in ['A4 landscape', '测试公司&lt;&amp;&gt;批发清单', '马达 发电机',
                     '测试客户', '客户地址', '配件编码', '品牌', '备注', '地址：测试路8号',
                     '电话：12345678', '客户签名：', '人民币（大写）：壹佰叁拾捌元整',
                     '138.00', '已收：¥1.00', '未收：¥137.00', '<td>6</td>']:
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertEqual(page.split('<tbody>')[1].split('</tbody>')[0].count('<tr>'), 1)
        self.assertNotIn('测试公司<&>', page)

    def test_more_than_six_items_are_preserved(self):
        page = self.print_order([(self.product, 1, 2300)] * 9)
        self.assertEqual(page.split('<tbody>')[1].split('</tbody>')[0].count('<tr>'), 9)
        self.assertIn('<td>9</td>', page)

    def test_uppercase_amounts(self):
        cases = {0: '零元整', 1: '零元壹分', 10: '零元壹角', 101: '壹元零壹分',
                 27200: '贰佰柒拾贰元整', 100100: '壹仟零壹元整',
                 1000100: '壹万零壹元整', 10000000100: '壹亿零壹元整',
                 102030: '壹仟零贰拾元叁角'}
        for cents, expected in cases.items():
            with self.subTest(cents=cents):
                self.assertEqual(self.app.money_upper(cents), expected)

    def test_delete_preserves_history_and_blocks_future_transactions(self):
        sale_id = self.app.create_sale(self.customer, [(self.product, 100, 2300)])
        self.app.delete_product(self.product)
        self.assertEqual(self.app.query('SELECT active FROM products')[0]['active'], 0)
        self.assertEqual(len(self.app.query('SELECT * FROM sale_items')), 1)
        self.assertEqual(len(self.app.query('SELECT * FROM inventory_movements')), 2)
        self.app.receive_payment(sale_id, 100)
        with patch.object(self.app.webbrowser, 'open'):
            self.app.App.print_sale(None, sale_id)
        for action in (lambda: self.app.create_purchase(self.product, 1, 100),
                       lambda: self.app.create_sale(self.customer, [(self.product, 1, 2300)])):
            with self.assertRaises(ValueError):
                action()

    def test_cannot_delete_stock_or_missing_product(self):
        with self.assertRaisesRegex(ValueError, '仍有库存'):
            self.app.delete_product(self.product)
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.app.delete_product(-1)
        self.assertEqual(self.app.query('SELECT stock,active FROM products')[0]['stock'], 100)
        self.assertEqual(self.app.query('SELECT stock,active FROM products')[0]['active'], 1)

    def test_inline_customer_selects_new_customer_and_preserves_cart(self):
        cart = [(self.product, 2, 2300)]
        screen = SimpleNamespace(customer_choices={}, sale_customer=Mock(), cart=cart, paid=Mock())

        def refresh():
            screen.customer_choices = {f"{r['id']} | {r['name']}": r['id']
                                       for r in self.app.query('SELECT * FROM customers')}

        screen.refresh_all = Mock(side_effect=refresh)
        customer_id = self.app.App.create_sale_customer(screen, ' 新客户 ', ' 123 ', ' 地址 ')
        screen.sale_customer.set.assert_called_once_with(f'{customer_id} | 新客户')
        self.assertEqual(screen.cart, cart)
        self.assertEqual(screen.paid.mock_calls, [])
        customer = self.app.query('SELECT * FROM customers WHERE id=?', (customer_id,))[0]
        self.assertEqual((customer['name'], customer['phone'], customer['address']), ('新客户', '123', '地址'))
        sale_id = self.app.create_sale(customer_id, cart)
        self.assertEqual(self.app.query('SELECT customer_id FROM sales WHERE id=?', (sale_id,))[0][0], customer_id)

    def test_customer_validation(self):
        with self.assertRaisesRegex(ValueError, '客户名称'):
            self.app.create_customer('   ')
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.app.create_customer('测试客户')
        self.assertEqual(len(self.app.query('SELECT * FROM customers')), 1)

    def test_batch_delete_is_atomic(self):
        self.app.create_product('零库存商品甲')
        self.app.create_product('零库存商品乙')
        empty_ids = [r['id'] for r in self.app.query('SELECT id FROM products WHERE stock=0')]
        with self.assertRaisesRegex(ValueError, '仍有库存'):
            self.app.delete_products(empty_ids + [self.product])
        self.assertEqual(len(self.app.query('SELECT id FROM products WHERE active=1')), 3)
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.app.delete_products(empty_ids + [-1])
        self.assertEqual(len(self.app.query('SELECT id FROM products WHERE active=1')), 3)
        self.app.delete_products(empty_ids + empty_ids)
        self.assertEqual([r['id'] for r in self.app.query('SELECT id FROM products WHERE active=1')], [self.product])
        with self.assertRaisesRegex(ValueError, '勾选'):
            self.app.delete_products([])

    def test_database_connection_closes_and_rolls_back(self):
        with self.app.db() as connection:
            connection.execute('SELECT 1')
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        with self.assertRaises(ValueError):
            with self.app.db() as connection:
                connection.execute("INSERT INTO customers(name) VALUES('回滚测试')")
                raise ValueError('rollback')
        self.assertEqual(self.app.query("SELECT * FROM customers WHERE name='回滚测试'"), [])
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')

    def test_price_update_preserves_historical_sale(self):
        sale_id = self.app.create_sale(self.customer, [(self.product, 1, 2300)])
        self.app.update_product_price(self.product, 2600)
        self.assertEqual(self.app.query('SELECT sale_price FROM products')[0][0], 2600)
        self.assertEqual(self.app.query('SELECT unit_price FROM sale_items WHERE sale_id=?', (sale_id,))[0][0], 2300)
        with self.assertRaises(ValueError):
            self.app.update_product_price(self.product, -1)
        with self.assertRaises(ValueError):
            self.app.update_product_price(-1, 100)

    def test_customer_update_preserves_debt_and_rejects_duplicate(self):
        sale_id = self.app.create_sale(self.customer, [(self.product, 1, 2300)])
        self.app.update_customer(self.customer, ' 新名称 ', ' 123 ', ' 新地址 ')
        customer = self.app.query('SELECT * FROM customers WHERE id=?', (self.customer,))[0]
        self.assertEqual((customer['name'], customer['phone'], customer['address']), ('新名称', '123', '新地址'))
        self.app.receive_payment(sale_id, 100)
        self.app.create_customer('其他客户')
        for name in (' ', '其他客户'):
            with self.assertRaises(ValueError):
                self.app.update_customer(self.customer, name)
        self.assertEqual(self.app.query('SELECT name FROM customers WHERE id=?', (self.customer,))[0][0], '新名称')

    def test_edit_cart_checks_combined_stock_and_preserves_failed_edit(self):
        screen = SimpleNamespace(cart=[(self.product, 60, 2300), (self.product, 30, 2300)], redraw_cart=Mock())
        self.app.App.update_cart_line(screen, 0, self.product, 70, 2500)
        self.assertEqual(screen.cart[0], (self.product, 70, 2500))
        screen.redraw_cart.assert_called_once()
        before = list(screen.cart)
        for qty, price in ((71, 2500), (0, 2500), (1, -1)):
            with self.assertRaises(ValueError):
                self.app.App.update_cart_line(screen, 0, self.product, qty, price)
            self.assertEqual(screen.cart, before)
        self.app.create_product('替换商品', '件')
        other = self.app.query('SELECT id FROM products WHERE name=?', ('替换商品',))[0][0]
        self.app.create_purchase(other, 5, 100)
        self.app.App.update_cart_line(screen, 0, other, 5, 500)
        self.assertEqual(screen.cart[0], (other, 5, 500))
        self.assertEqual(self.app.query('SELECT stock FROM products WHERE id=?', (other,))[0][0], 5)

    def test_product_checkboxes_toggle_and_select_all(self):
        selected = set()
        tree = Mock()
        tree.get_children.return_value = ('1', '2')
        tree.selection.side_effect = lambda: tuple(selected)
        tree.selection_add.side_effect = lambda row: selected.add(row)
        tree.selection_remove.side_effect = lambda *rows: selected.difference_update(rows)

        def set_selection(rows):
            selected.clear()
            selected.update(rows)

        tree.selection_set.side_effect = set_selection
        screen = SimpleNamespace(product_tree=tree)
        screen.sync_product_checks = lambda: self.app.App.sync_product_checks(screen)
        self.app.App.toggle_product_row(screen, '1')
        self.assertEqual(screen.checked_product_ids, {'1'})
        tree.item.assert_any_call('1', text='☑')
        tree.item.assert_any_call('2', text='☐')
        self.app.App.toggle_product_row(screen, '1')
        self.assertEqual(screen.checked_product_ids, set())
        self.app.App.toggle_all_products(screen)
        self.assertEqual(screen.checked_product_ids, {'1', '2'})
        self.app.App.toggle_all_products(screen)
        self.assertEqual(screen.checked_product_ids, set())


if __name__ == '__main__':
    unittest.main()
