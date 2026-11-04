"""Daily targets use synthetic aggregates, temporary preferences and no network."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QDate, QEvent, QObject, QSettings, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QDoubleSpinBox

from time_logger.desktop_controller import DesktopController, LOCAL_TZ
from time_logger.day_window import recording_date
from time_logger.desktop_ui import DesktopWindow, COLORS
from tests.test_desktop_floating import FakeController

APP = QApplication.instance() or QApplication([])


def sample(work=7200, study=1800):
    now = datetime.now(LOCAL_TZ)
    day = recording_date(now).isoformat()
    return {"date": day, "day_start_hour": 7, "month": day[:7], "as_of_ms": int(now.timestamp() * 1000),
            "daily_goals": {"source": "feishu", "date": day,
                "work_hours": 2, "study_hours": 1, "as_of_ms": int(now.timestamp() * 1000),
                "settings_url": "https://my.feishu.cn/base/synthetic?table=synthetic",
                "cache": {"state": "fresh"}},
            "cache": {"state": "fresh"}, "days": [{"date": day,
            "categories": {"工作": {"recorded_seconds": work, "estimated_seconds": 0},
                           "学习": {"recorded_seconds": study, "estimated_seconds": 0}}}]}


class ProgressController(FakeController):
    daily_progress = Signal(dict)
    daily_progress_state = Signal(str)


class DailyProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = QSettings(str(self.path / "ui.ini"), QSettings.Format.IniFormat)
        self.controller = ProgressController()
        self.targets = (2, 1)
        self.window = DesktopWindow(self.controller, settings=self.settings, create_tray=False)

    def tearDown(self):
        self.window._closing = True
        self.window.close()
        self.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.temp.cleanup()

    def publish(self, value=None, state=""):
        value = copy.deepcopy(sample() if value is None else value)
        if "daily_goals" in value:
            value["daily_goals"].update(work_hours=self.targets[0], study_hours=self.targets[1])
        self.controller.daily_progress.emit(value)
        self.controller.daily_progress_state.emit(state)

    def goals(self, work=2, study=1):
        self.targets = (work, study)

    def test_unset_targets_never_claim_success(self):
        self.goals(0, 0)
        self.publish()
        for bar, status, _ in self.window.goal_rows.values():
            self.assertEqual(bar.value(), 0)
            self.assertIn("未设目标", bar.format())
            self.assertEqual(status.text(), "● 未设置")

    def test_targets_are_independent_and_green_only_at_threshold(self):
        self.goals()
        self.publish()
        work, work_state, _ = self.window.goal_rows["work"]
        study, study_state, _ = self.window.goal_rows["study"]
        self.assertEqual((work.value(), study.value()), (1000, 500))
        self.assertEqual(work_state.text(), "● 达标")
        self.assertIn(COLORS["primary"], work_state.styleSheet())
        self.assertEqual(study_state.text(), "● 未达标")
        self.publish(sample(work=7199, study=7200))
        self.assertEqual(work_state.text(), "● 未达标")
        self.assertEqual(study_state.text(), "● 达标")
        self.assertEqual(study.value(), 1000)

    def test_eight_and_three_hour_targets_are_inclusive(self):
        self.goals(8, 3)
        self.publish(sample(work=28800, study=10800))
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 达标")
        self.assertEqual(self.window.goal_rows["study"][1].text(), "● 达标")
        self.publish(sample(work=28799, study=10799))
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 未达标")
        self.assertEqual(self.window.goal_rows["study"][1].text(), "● 未达标")

    def test_unknown_duration_is_not_zero_and_estimates_are_marked(self):
        self.goals()
        value = sample(work=None, study=1800)
        value["days"][0]["categories"]["学习"]["estimated_seconds"] = 600
        self.publish(value)
        self.assertIn("暂无时长", self.window.goal_rows["work"][0].format())
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待记录")
        self.assertTrue(self.window.goal_rows["study"][0].format().startswith("约 "))
        self.assertIn("含估算", self.window.goal_rows["study"][0].toolTip())

    def test_cached_expired_and_failed_reads_do_not_show_green(self):
        self.goals()
        for cache in ({"local_cached": True}, {"state": "stale"}, {"stale": True}):
            value = sample()
            value["cache"] = cache
            self.publish(value)
            self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待刷新")
        value = sample()
        value["as_of_ms"] -= 180000
        self.publish(value)
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待刷新")
        self.publish(state="进度暂未更新")
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待刷新")
        self.publish()
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 达标")

    def test_history_browsing_and_date_rollover_do_not_reuse_yesterday(self):
        self.goals()
        self.publish()
        self.window.date_edit.setDate(QDate.currentDate().addDays(-1))
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 达标")
        value = sample()
        value["days"][0]["date"] = (datetime.now(LOCAL_TZ).date() - timedelta(days=1)).isoformat()
        self.publish(value)
        self.assertEqual(self.window.goal_rows["work"][0].value(), 0)
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待记录")

    def test_local_targets_are_ignored_and_missing_feishu_never_falls_back(self):
        self.settings.setValue("goals/work_hours", 0.5)
        self.publish(sample(work=3600))
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 未达标")
        value = sample()
        value.pop("daily_goals")
        self.publish(value)
        self.assertIn("目标未读取", self.window.goal_rows["work"][0].format())
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待读取")

    def test_stale_or_wrong_day_goals_never_show_green(self):
        for change in ({"cache": {"state": "stale"}}, {"as_of_ms": sample()["as_of_ms"] - 180000}):
            value = sample()
            value["daily_goals"].update(change)
            self.publish(value)
            self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待刷新")
        value = sample()
        value["daily_goals"]["date"] = "2000-01-01"
        self.publish(value)
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待读取")

    def test_targets_dialog_is_read_only_and_opens_feishu(self):
        self.publish()
        self.settings.setValue("goals/work_hours", 9)
        def finish():
            dialog = APP.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            self.assertFalse(dialog.findChildren(QDoubleSpinBox))
            from PySide6.QtWidgets import QPushButton
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "在飞书修改目标")
            button.click()
            dialog.reject()
        with patch("time_logger.desktop_ui.QDesktopServices.openUrl") as opened:
            QTimer.singleShot(0, finish)
            self.window.edit_daily_goals()
            self.assertEqual(opened.call_count, 1)
            self.assertIn("synthetic", opened.call_args.args[0].toString())
        self.assertEqual(self.settings.value("goals/work_hours", type=float), 9)

    def test_seven_am_rollover_follows_today_but_preserves_manual_browsing(self):
        from datetime import date
        old, new = date(2026, 12, 31), date(2027, 1, 1)
        self.window._today = old.isoformat()
        self.window.date_edit.setDate(QDate(2026, 12, 31))
        with patch("time_logger.desktop_ui.recording_date", return_value=new):
            self.window._day_tick()
        self.assertEqual(self.window.selected_date(), new.isoformat())
        self.window._today = old.isoformat()
        self.window.date_edit.setDate(QDate(2026, 12, 20))
        with patch("time_logger.desktop_ui.recording_date", return_value=new):
            self.window._day_tick()
        self.assertEqual(self.window.selected_date(), "2026-12-20")

    def test_natural_day_cache_cannot_show_a_seven_am_success(self):
        value = sample()
        value["day_start_hour"] = 0
        self.publish(value)
        self.assertEqual(self.window.goal_rows["work"][1].text(), "● 待读取")


class DailyProgressReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = Mock()
        self.controller = DesktopController({}, Path(self.temp.name) / "state.sqlite3", self.transport)
        self.values, self.states = [], []
        self.controller.daily_progress.connect(self.values.append)
        self.controller.daily_progress_state.connect(self.states.append)

    def tearDown(self):
        self.controller.stop()
        self.temp.cleanup()

    def test_reads_current_activity_day_independently_of_browsing_and_caches(self):
        self.controller.day = "2026-01-01"
        value = sample()
        self.transport.request.return_value = {"history": value}
        self.controller._refresh_daily_progress()
        self.transport.request.assert_called_once_with("history", date=value["date"], day_start_hour=7, limit=1)
        self.assertEqual(self.values, [value])
        self.assertEqual(self.states[-1], "")
        self.assertFalse(self.controller.daily_progress_requested)
        self.assertEqual(self.controller.local.preference("daily_progress:start:7"), value)

    def test_reopen_labels_existing_cache_and_keeps_refresh_requested(self):
        value = sample()
        self.controller.local.set_preference("daily_progress:start:7", value)
        self.controller.set_visible(True)
        self.assertTrue(self.values[-1]["cache"]["local_cached"])
        self.assertEqual(self.states[-1], "缓存待刷新")
        self.assertTrue(self.controller.daily_progress_requested)
        self.transport.request.assert_not_called()

    def test_wrong_day_or_boundary_does_not_publish_or_replace_cache(self):
        original = sample()
        key = "daily_progress:start:7"
        self.controller.local.set_preference(key, original)
        for change in ({"date": "2000-01-01"}, {"days": []}, {"day_start_hour": 0}):
            value = {**original, **change}
            self.transport.request.return_value = {"history": value}
            with self.assertRaises(ValueError):
                self.controller._refresh_daily_progress()
        self.assertFalse(self.values)
        self.assertEqual(self.controller.local.preference(key), original)

    def test_stale_response_preserves_last_good_cache_and_requests_refresh(self):
        original = sample()
        key = "daily_progress:start:7"
        self.controller.local.set_preference(key, original)
        stale = copy.deepcopy(original)
        stale["cache"] = {"state": "stale", "refresh_error": "offline"}
        self.transport.request.return_value = {"history": stale}
        self.controller._refresh_daily_progress()
        self.assertEqual(self.states[-1], "缓存待刷新")
        self.assertTrue(self.controller.daily_progress_requested)
        self.assertEqual(self.controller.local.preference(key), original)


if __name__ == "__main__":
    unittest.main()
