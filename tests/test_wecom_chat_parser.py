from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from autoim.wecom.chat_parser import ParseResult, parse_wecom_clipboard


def header(sender: str = "崔玮杰@微信@微信联系人", time: str = "11/22 17:39:29") -> str:
    return f"{sender} {time}\n\n你好"


class WeComClipboardParserTests(unittest.TestCase):
    def test_01_verified_three_message_sample(self):
        text = ("崔玮杰@微信@微信联系人 11/22 17:39:29\n\n你好\n\n"
                "崔玮杰@微信@微信联系人 11/22 17:39:37\n\n我想了解医疗险\n\n"
                "崔玮杰@微信@微信联系人 11/22 17:39:44\n\n今年35岁，有什么产品？")
        result = parse_wecom_clipboard(text)
        self.assertEqual(len(result.messages), 3)
        self.assertEqual([m.display_name for m in result.messages], ["崔玮杰"] * 3)
        self.assertEqual([m.content for m in result.messages], ["你好", "我想了解医疗险", "今年35岁，有什么产品？"])
        self.assertTrue(all(m.peer_type == "PERSONAL_WECHAT" for m in result.messages))

    def test_02_single_ordinary_message(self):
        self.assertEqual(len(parse_wecom_clipboard(header()).messages), 1)

    def test_03_ten_consecutive_messages(self):
        text = "\n\n".join(f"A@微信 11/22 10:00:{i:02d}\n\n正文{i}" for i in range(10))
        self.assertEqual(len(parse_wecom_clipboard(text).messages), 10)

    def test_04_duplicate_bodies_are_not_deduplicated(self):
        text = "A@微信 11/22 10:00:00\n\n你好\n\nA@微信 11/22 10:00:05\n\n你好"
        self.assertEqual([m.content for m in parse_wecom_clipboard(text).messages], ["你好", "你好"])

    def test_05_same_timestamp_messages_are_not_deduplicated(self):
        text = "A@微信 11/22 10:00:00\n\n甲\n\nB@微信 11/22 10:00:00\n\n乙"
        self.assertEqual(len(parse_wecom_clipboard(text).messages), 2)

    def test_06_alternating_senders_keep_order(self):
        text = "A@微信 11/22 10:00:00\n\n甲\n\nB@微信 11/22 10:00:01\n\n乙\n\nA@微信 11/22 10:00:02\n\n丙"
        self.assertEqual([m.display_name for m in parse_wecom_clipboard(text).messages], ["A", "B", "A"])

    def test_07_personal_wechat(self):
        message = parse_wecom_clipboard(header("崔玮杰@微信@微信联系人")).messages[0]
        self.assertEqual((message.display_name, message.peer_type, message.organization_name), ("崔玮杰", "PERSONAL_WECHAT", None))

    def test_08_external_wecom(self):
        message = parse_wecom_clipboard(header("崔玮杰@厦门凯南展示制品有限公司")).messages[0]
        self.assertEqual((message.display_name, message.peer_type, message.organization_name), ("崔玮杰", "EXTERNAL_WECOM", "厦门凯南展示制品有限公司"))

    def test_09_name_alone_does_not_prove_internal_employee(self):
        message = parse_wecom_clipboard(header("王蕊")).messages[0]
        self.assertEqual(message.peer_type, "UNKNOWN")

    def test_10_company_name_containing_wechat_is_not_personal_marker(self):
        message = parse_wecom_clipboard(header("王蕊@微信科技有限公司")).messages[0]
        self.assertEqual(message.peer_type, "EXTERNAL_WECOM")
        self.assertEqual(message.organization_name, "微信科技有限公司")

    def test_11_fullwidth_and_ascii_at_sign(self):
        self.assertEqual(parse_wecom_clipboard(header("甲＠微信联系人")).messages[0].peer_type, "PERSONAL_WECHAT")
        self.assertEqual(parse_wecom_clipboard(header("甲@微信联系人")).messages[0].peer_type, "PERSONAL_WECHAT")

    def test_12_combined_wechat_labels_are_preserved(self):
        message = parse_wecom_clipboard(header()).messages[0]
        self.assertEqual(message.raw_sender, "崔玮杰@微信@微信联系人")
        self.assertEqual(message.peer_type, "PERSONAL_WECHAT")

    def test_13_multiline_body_preserved(self):
        text = "A@微信 11/22 10:00:00\n\n第一行\n第二行"
        self.assertEqual(parse_wecom_clipboard(text).messages[0].content, "第一行\n第二行")

    def test_14_internal_blank_line_preserved(self):
        text = "A@微信 11/22 10:00:00\n\n第一段\n\n第二段"
        self.assertEqual(parse_wecom_clipboard(text).messages[0].content, "第一段\n\n第二段")

    def test_15_body_can_contain_date_and_time(self):
        text = "A@微信 11/22 10:00:00\n\n我在 11/22 17:39:29 开会"
        self.assertEqual(len(parse_wecom_clipboard(text).messages), 1)

    def test_16_header_shaped_body_is_reported_as_ambiguous(self):
        text = "A@微信 11/22 10:00:00\n\n正文\n\nB@微信 11/22 10:00:01\n\n这可能是正文，也可能是下一条"
        result = parse_wecom_clipboard(text)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[1].parse_status, "AMBIGUOUS_BOUNDARY")
        self.assertTrue(any("边界" in warning for warning in result.messages[1].parse_warnings))

    def test_17_crlf_newlines(self):
        raw = "A@微信 11/22 10:00:00\r\n\r\n你好"
        message = parse_wecom_clipboard(raw).messages[0]
        self.assertEqual(message.content, "你好")
        self.assertEqual(message.raw_message_block, raw)

    def test_18_lf_newlines(self):
        self.assertEqual(parse_wecom_clipboard("A@微信 11/22 10:00:00\n\n你好").messages[0].content, "你好")

    def test_19_year_is_not_invented(self):
        message = parse_wecom_clipboard(header()).messages[0]
        self.assertIsNone(message.parsed_datetime)
        self.assertFalse(message.date_year_reliable)

    def test_20_cross_date_messages_keep_source_order_and_raw_dates(self):
        text = "A@微信 11/30 23:59:59\n\n晚\n\nA@微信 12/01 00:00:01\n\n早"
        messages = parse_wecom_clipboard(text).messages
        self.assertEqual([m.raw_time for m in messages], ["11/30 23:59:59", "12/01 00:00:01"])
        self.assertEqual([m.sequence_number for m in messages], [1, 2])

    def test_21_incomplete_header_is_not_fabricated(self):
        result = parse_wecom_clipboard("A@微信 11/22 10:00\n\n你好")
        self.assertEqual(result.parse_status, "UNRECOGNIZED")
        self.assertEqual(result.messages, ())

    def test_22_unknown_identity_tag_is_unknown(self):
        self.assertEqual(parse_wecom_clipboard(header("A@陌生标签")).messages[0].peer_type, "UNKNOWN")

    def test_23_same_name_does_not_create_same_customer_id(self):
        text = "张三@微信@微信联系人 11/22 10:00:00\n\n甲\n\n张三@微信@微信联系人 11/22 10:00:01\n\n乙"
        messages = parse_wecom_clipboard(text).messages
        self.assertEqual([m.display_name for m in messages], ["张三", "张三"])
        self.assertIsNone(messages[0].contact_id)
        self.assertIsNone(messages[1].conversation_id)

    def test_24_empty_clipboard(self):
        self.assertEqual(parse_wecom_clipboard("").parse_status, "EMPTY")

    def test_25_plain_non_chat_text_is_retained(self):
        text = "普通文本，没有聊天记录格式"
        result = parse_wecom_clipboard(text)
        self.assertEqual(result.parse_status, "UNRECOGNIZED")
        self.assertEqual(result.raw_text, text)

    def test_26_header_without_blank_separator_is_not_split(self):
        result = parse_wecom_clipboard("A@微信 11/22 10:00:00\n你好\nB@微信 11/22 10:00:01\n正文")
        self.assertEqual(len(result.messages), 0)
        self.assertEqual(result.parse_status, "UNRECOGNIZED")
        self.assertEqual(result.raw_text, "A@微信 11/22 10:00:00\n你好\nB@微信 11/22 10:00:01\n正文")
        self.assertTrue(any("类似消息头" in warning for warning in result.warnings))

    def test_27_chinese_and_special_symbols_preserved(self):
        content = "您好，🎉！￥@#&<>\t结束"
        message = parse_wecom_clipboard(f"A@微信 11/22 10:00:00\n\n{content}").messages[0]
        self.assertEqual(message.content, content)

    def test_28_very_long_input_is_kept(self):
        content = "长" * 1_100_000
        result = parse_wecom_clipboard(f"A@微信 11/22 10:00:00\n\n{content}")
        self.assertEqual(len(result.messages[0].content), len(content))
        self.assertEqual(len(result.raw_text), len(content) + len("A@微信 11/22 10:00:00\n\n"))

    def test_29_gui_can_parse_same_clipboard_repeatedly(self):
        from PySide6.QtWidgets import QApplication
        from autoim.app import LogBus, MainWindow

        app = QApplication.instance() or QApplication([])
        raw = header()
        parsed = parse_wecom_clipboard(raw)
        outcome = {"raw_text": raw, "result": parsed, "snapshot": object()}
        window = MainWindow(LogBus())
        window.clipboard_parse_finished(outcome)
        first = window.parse_table.rowCount()
        window.clipboard_parse_finished(outcome)
        self.assertEqual(first, 1)
        self.assertEqual(window.parse_table.rowCount(), 1)
        window.close()
        app.processEvents()

    def test_30_failed_parse_keeps_original_text(self):
        raw = "原始文本\n\n不应丢失"
        result = ParseResult(raw, (), "ERROR", ("故意构造的解析失败",))
        self.assertEqual(result.raw_text, raw)
        self.assertEqual(result.messages, ())

    def test_direction_and_official_ids_remain_unverified(self):
        message = parse_wecom_clipboard(header()).messages[0]
        self.assertEqual(message.direction, "UNKNOWN")
        self.assertIsNone(message.contact_id)
        self.assertIsNone(message.conversation_id)

    def test_conflicting_personal_and_company_markers_are_unknown(self):
        message = parse_wecom_clipboard(header("A@微信@示例有限公司")).messages[0]
        self.assertEqual(message.peer_type, "UNKNOWN")
        self.assertIsNone(message.organization_name)

    def test_each_record_has_a_temporary_local_id_only(self):
        message = parse_wecom_clipboard(header()).messages[0]
        self.assertTrue(message.local_record_id)
        self.assertIsNone(message.contact_id)
        self.assertIsNone(message.conversation_id)


if __name__ == "__main__":
    unittest.main()
