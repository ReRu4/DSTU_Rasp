"""Бот с календарём, компактной навигацией и подпиской на несколько групп."""
from __future__ import annotations

from bot import MultiBot, save_state, KEYBOARD_VERSION, LOG
from navigation import NavigationMixin
from watch_groups import WatchGroupsMixin


RELEASE_VERSION = "2026-10-month-and-groups-v1"
RELEASE_TEXT = (
    "✨ Бот обновлён!\n\n"
    "📆 Появилось расписание на месяц: короткие названия, время, аудитории и окна.\n"
    "📅 Внизу теперь четыре кнопки: Сегодня, Неделя, Месяц и Настройки. "
    "Стрелки под расписанием листают даты, «Выбрать день» открывает календарь.\n"
    "🕒 Видно время последней проверки ДГТУ. При сбое сайта доступна сохранённая версия.\n"
    "⚙️ В настройках можно выбрать текст или картинку для дня и недели, "
    "включить отслеживание нескольких групп и отправку новых карточек при изменениях.\n\n"
    "Несколько групп и автоматические карточки изменений изначально выключены. "
    "Ваша основная группа, время рассылки и оформление сохранены."
)


class ScheduleBot(NavigationMixin, WatchGroupsMixin, MultiBot):
    def new_profile(self, channel: str, chat_id: int | None = None, legacy: bool = False) -> dict:
        profile = super().new_profile(channel, chat_id, legacy)
        # Новые пользователи сразу видят новый интерфейс. Перенесённые получают анонс.
        if not legacy:
            profile["last_release_notified"] = RELEASE_VERSION
        return profile

    def queue_release_notifications(self) -> bool:
        changed = False
        for profile in self.active_profiles():
            if (profile.get("channel") != "telegram" or profile.get("owner_chat_id")
                    or profile.get("onboarding") or not profile.get("group_name")
                    or profile.get("last_release_notified") == RELEASE_VERSION):
                continue
            if any(item.get("kind") == "release" and item.get("release_version") == RELEASE_VERSION
                   for item in profile["pending"]):
                continue
            profile["pending"].insert(0, {"kind": "release", "text": RELEASE_TEXT,
                                          "release_version": RELEASE_VERSION})
            changed = True
        if changed:
            save_state(self.state_file, self.state)
        return changed

    def queue_due(self, now) -> bool:
        notices = self.queue_release_notifications()
        return super().queue_due(now) or notices

    def ensure_telegram_keyboards(self) -> None:
        # Анонс установит новую клавиатуру своим сообщением без второго уведомления.
        for profile in self.active_profiles():
            if (profile.get("channel") == "telegram" and not profile.get("onboarding")
                    and profile.get("group_name") and profile.get("keyboard_version") != KEYBOARD_VERSION
                    and not any(item.get("kind") == "release" for item in profile["pending"])):
                try:
                    self.send_profile_message(profile, "Кнопки расписания: Сегодня, Неделя, Месяц и Настройки.")
                except Exception as exc:
                    LOG.warning("Не удалось обновить кнопки чату %s: %s", profile.get("chat_id"), exc)
