# -*- coding: utf-8 -*-
"""台账导入：xls / 表头定位 / Excel 日期序列。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ui.standard_ledger_page import StandardLedgerPage


class _ImportHelper:
    """不创建完整 Qt 页面，仅复用导入解析方法。"""

    _map_headers = StandardLedgerPage._map_headers
    _import_cell_str = staticmethod(StandardLedgerPage._import_cell_str)
    _rows_from_table = StandardLedgerPage._rows_from_table
    _row_from_mapped = StandardLedgerPage._row_from_mapped
    _read_import_file = StandardLedgerPage._read_import_file


class TestLedgerImportXls(unittest.TestCase):
    def setUp(self):
        self.helper = _ImportHelper()

    def test_header_scan_skips_title_row(self):
        table = [
            ['某公司受控标准清单', '', '', ''],
            ['档案号', '标准名称', '标准编号', '发布日期', '实施日期', '状态'],
            ['001', '安全带', 'GB/T 6096-2009', '2009-04-13', '2009-12-01', '现行'],
        ]
        rows = self.helper._rows_from_table(table)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['std_no'], 'GB/T 6096-2009')
        self.assertEqual(rows[0]['std_name'], '安全带')
        self.assertEqual(rows[0]['status'], '现行')
        self.assertEqual(rows[0]['publish_date'], '2009-04-13')

    def test_mgmt_no_from_header_alias(self):
        table = [
            ['标题行', '', '', '', '', '', ''],
            ['档案号', '规范受控管理编号', '标准名称', '标准编号', '发布日期', '实施日期', '状态'],
            ['1', 'JRJC-GLC16-001', '安全带', 'GB/T 6096-2009', '2009-04-13', '2009-12-01', '现行'],
        ]
        rows = self.helper._rows_from_table(table)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['mgmt_no'], 'JRJC-GLC16-001')
        self.assertEqual(rows[0]['std_no'], 'GB/T 6096-2009')

    def test_excel_serial_date_with_datemode(self):
        # 39916 ≈ 2009-04-13 (datemode=0)
        s = StandardLedgerPage._import_cell_str(39916.0, datemode=0)
        self.assertEqual(s, '2009-04-13')

    def test_real_jurun_xls_if_present(self):
        path = r'e:\Desktop\钜润规范目录.xls'
        if not os.path.isfile(path):
            self.skipTest('样例 xls 不存在')
        try:
            import xlrd  # noqa: F401
        except ImportError:
            self.skipTest('xlrd 未安装')
        rows = self.helper._read_import_file(path)
        self.assertGreater(len(rows), 100)
        self.assertTrue(rows[0]['std_no'])
        self.assertTrue(rows[0]['std_name'])
        with_mgmt = [r for r in rows if (r.get('mgmt_no') or '').startswith('JRJC')]
        self.assertGreater(len(with_mgmt), 0, '应导入管理编号 JRJC-…')
        # 日期应已转为 YYYY-MM-DD
        if rows[0].get('publish_date'):
            self.assertRegex(rows[0]['publish_date'], r'^\d{4}-\d{2}-\d{2}$')


if __name__ == '__main__':
    unittest.main()
