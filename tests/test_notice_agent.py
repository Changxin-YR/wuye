import unittest
import json
from pathlib import Path
from unittest.mock import patch

import test_app as fixtures
from dify_client import BailianClient
from models import Community, Notice
from sqlalchemy import select

from agent_planner import plan_request


class NoticePlannerTests(unittest.TestCase):
    authorized = {"notice.save", "notice.read"}

    def test_natural_notice_requests_plan_save_with_scope_and_content(self):
        cases = (
            ("发布一个公告：明天上午8点到12点停水", "community"),
            ("帮我发布一个全小区停水公告", "community"),
            ("通知所有业主明天停电检修", "community"),
            ("给A栋发个通知，今晚10点以后停电", "building"),
        )
        for message, scope in cases:
            with self.subTest(message=message):
                plan = plan_request(message, self.authorized)
                self.assertEqual(plan["intent"], "notice.save")
                self.assertEqual(plan["action"], "TOOL")
                self.assertEqual(plan["arguments"]["notice_scope"], scope)
                self.assertTrue(plan["arguments"].get("notice_content"))

    def test_explicit_title_and_content_are_preserved(self):
        plan = plan_request(
            "帮我发布公告，标题是电梯检修，内容是2号电梯明天上午维修",
            self.authorized,
        )
        self.assertEqual(plan["intent"], "notice.save")
        self.assertEqual(plan["arguments"]["notice_title"], "电梯检修")
        self.assertEqual(plan["arguments"]["notice_content"], "2号电梯明天上午维修")

    def test_spoken_notice_prefix_is_removed_from_content(self):
        plan = plan_request("给三栋发个电梯维修通知", self.authorized)
        self.assertEqual(plan["arguments"]["notice_content"], "电梯维修")
        plan = plan_request("阳光花园发个公告，周末清洗水箱", self.authorized)
        self.assertEqual(plan["arguments"]["notice_content"], "周末清洗水箱")

    def test_chinese_building_numbers_have_persisted_name_variants(self):
        from agent_planner import building_name_variants
        self.assertIn("3栋", building_name_variants("三栋"))

    def test_title_and_content_without_publish_verb_are_notice_save(self):
        plan = plan_request("公告标题是电梯检修，内容是2号电梯明天上午维修", self.authorized)
        self.assertEqual(plan["intent"], "notice.save")

    def test_multiple_writable_communities_require_scope_clarification(self):
        plan = plan_request(
            "发布公告：明天停水",
            {"notice.save"},
            {"writable_communities": [{"id": 1, "name": "A小区"}, {"id": 2, "name": "B小区"}]},
        )
        self.assertEqual(plan["action"], "CLARIFY")
        self.assertIn("community_id", plan["missing_fields"])

    def test_explicit_community_name_is_extracted_without_command_prefix(self):
        plan = plan_request(
            "发到A小区的全区停水公告",
            self.authorized,
            {"writable_communities": [{"id": 1, "name": "A小区"}, {"id": 2, "name": "B小区"}]},
        )
        self.assertEqual(plan["action"], "TOOL")
        self.assertEqual(plan["arguments"]["community_name"], "A小区")
        self.assertEqual(plan["arguments"]["community_id"], 1)

    def test_all_responsible_communities_is_confirmed_batch_intent(self):
        plan = plan_request(
            "给我负责的所有小区发布公告：明天停水",
            {"notice.save", "notice.batch_publish"},
            {"writable_communities": [{"id": 1, "name": "A小区"}, {"id": 2, "name": "B小区"}]},
        )
        self.assertEqual(plan["intent"], "notice.batch_publish")
        self.assertEqual(plan["action"], "CONFIRM")

    def test_real_world_fixture_has_one_hundred_tasks_and_notice_coverage(self):
        rows = json.loads((Path(__file__).parent / "fixtures" / "real_agent_tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 100)
        self.assertGreaterEqual(sum(row["category"] == "公告" for row in rows), 15)

    def test_notice_follow_up_requires_real_context(self):
        self.assertEqual(
            plan_request("撤掉刚才发布的公告", {"notice.archive"})["action"],
            "CLARIFY",
        )
        self.assertEqual(
            plan_request("把刚才那个公告改一下，时间改成下午三点", {"notice.save"})["action"],
            "CLARIFY",
        )
        plan = plan_request(
            "把刚才那个公告改一下，时间改成下午三点",
            {"notice.save"},
            {"resolved_notice": {"id": 7, "title": "停水公告"}, "writable_communities": [{"id": 1, "name": "A小区"}]},
        )
        self.assertEqual(plan["intent"], "notice.save")
        self.assertEqual(plan["arguments"]["notice_id"], 7)
        self.assertEqual(plan["arguments"]["notice_content"], "下午三点")

    def test_non_notice_updates_do_not_route_to_notice_save(self):
        plan = plan_request("把他的手机号改成13800000123", {"notice.save", "person.save"})
        self.assertEqual(plan["intent"], "person.save")
        order = plan_request("发布一个维修工单", {"notice.save", "order.create"})
        self.assertEqual(order["intent"], "order.create")


class NoticeChatTests(unittest.TestCase):
    for _method in ("setUp", "tearDown", "csrf", "post", "login", "json_post"):
        locals()[_method] = getattr(fixtures.PropertyAppContractTests, _method)

    def test_chat_fallback_builds_and_executes_notice_save(self):
        self.login("admin")
        client = BailianClient("http://agent.invalid", "fixture-key", "qwen-plus")
        responses = iter([
            {"id": "one", "choices": [{"message": {"content": "我来发布"}}]},
            {"id": "two", "choices": [{"message": {"content": "公告已发布"}}]},
        ])
        self.app.extensions["dify"] = client
        with patch.object(client, "_request", side_effect=lambda *args, **kwargs: next(responses)):
            response = self.json_post("/ai/chat", {"message": "发布一个公告：明天上午8点到12点停水"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with self.db() as db:
            notice = db.scalar(select(Notice))
            self.assertEqual((notice.community_id, notice.building_id), (1, None))
            self.assertEqual(notice.title, "停水公告")
            self.assertEqual(notice.content, "明天上午8点到12点停水")

    def test_chat_resolves_real_building_for_building_notice(self):
        self.login("admin")
        client = BailianClient("http://agent.invalid", "fixture-key", "qwen-plus")
        responses = iter([
            {"id": "one", "choices": [{"message": {"content": "我来发布"}}]},
            {"id": "two", "choices": [{"message": {"content": "公告已发布"}}]},
        ])
        self.app.extensions["dify"] = client
        with patch.object(client, "_request", side_effect=lambda *args, **kwargs: next(responses)):
            response = self.json_post("/ai/chat", {"message": "给A栋发个通知，今晚10点以后停电"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with self.db() as db:
            notice = db.scalar(select(Notice))
            self.assertEqual((notice.community_id, notice.building_id), (1, 1))
            self.assertEqual(notice.content, "今晚10点以后停电")

    def test_chat_uses_one_confirmed_batch_tool_for_all_responsible_communities(self):
        self.login("admin")
        with self.db() as db:
            db.add(Community(name="第二小区", address="测试地址", phone="057100000000"))
            db.commit()
        client = BailianClient("http://agent.invalid", "fixture-key", "qwen-plus")
        responses = iter([
            {"id": "one", "choices": [{"message": {"content": "我来准备"}}]},
            {"id": "two", "choices": [{"message": {"content": "请在确认卡片中确认"}}]},
        ])
        self.app.extensions["dify"] = client
        with patch.object(client, "_request", side_effect=lambda *args, **kwargs: next(responses)):
            response = self.json_post("/ai/chat", {"message": "给我负责的所有小区发布公告：明天停水"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(response.json["actions"]), 1)
        self.assertEqual(response.json["actions"][0]["command"], "notice.batch_publish")
        self.assertEqual(response.json["actions"][0]["status"], "pending")
        with self.db() as db:
            self.assertEqual(db.query(Notice).count(), 0)
        confirmed = self.json_post("/ai/actions/" + response.json["actions"][0]["id"] + "/confirm", {})
        self.assertEqual(confirmed.status_code, 200, confirmed.get_data(as_text=True))
        with self.db() as db:
            self.assertEqual(db.query(Notice).count(), 2)

    def test_missing_building_is_clarified_without_creating_master_data(self):
        self.login("admin")
        response = self.json_post("/ai/chat", {"message": "给不存在的9栋发个通知，今晚停电"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["source"], "planner")
        self.assertEqual(response.json["actions"], [])
        with self.db() as db:
            from models import Building
            self.assertIsNone(db.scalar(select(Building).where(Building.name == "9")))
            self.assertIsNone(db.scalar(select(Notice)))


if __name__ == "__main__":
    unittest.main()
