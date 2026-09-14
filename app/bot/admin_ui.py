"""Owner administration screens rendered entirely inside the MAX bot."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import Actor
from app.bot.admin import (
    ROLE_KEYS,
    add_staff,
    get_bot_settings,
    get_staff,
    list_group_chats,
    list_staff,
    require_owner,
    set_global_group_analysis,
    set_group_chat_analysis,
    set_staff_active,
    set_staff_role,
)
from app.bot.ui import Buttons, button, reply
from app.bot.updates import IncomingUpdate
from app.core.config import Settings
from app.crm.errors import CRMInvalidReference
from app.models.bot import BotConversation, BotGroupChat

PAGE_SIZE = 8
ADMIN_ACTIONS = {
    "admin",
    "admin_staff",
    "admin_staff_card",
    "admin_staff_add",
    "admin_staff_add_role",
    "admin_staff_role",
    "admin_staff_active",
    "admin_roles",
    "admin_chats",
    "admin_chat",
    "admin_chat_toggle",
    "admin_settings",
    "admin_settings_group",
}


def owner_admin_menu() -> Buttons:
    return [
        [button("Сотрудники", "admin_staff:0"), button("Роли", "admin_roles")],
        [button("Чаты", "admin_chats:0"), button("Настройки", "admin_settings")],
        [button("Диспетчерская", "menu")],
    ]


async def show_owner_admin(db: AsyncSession, actor: Actor) -> None:
    await reply(
        db,
        actor,
        "Управление\nСотрудники, роли, групповые чаты и бизнес-настройки бота.",
        owner_admin_menu(),
    )


async def _staff_card(
    db: AsyncSession,
    settings: Settings,
    actor: Actor,
    organization_id: UUID,
    employee_id: UUID,
) -> None:
    employee, role = await get_staff(db, organization_id, employee_id)
    protected = employee.max_user_id in settings.max_owner_ids
    lines = [
        employee.display_name,
        f"MAX ID: {employee.max_user_id}",
        f"Роль: {'Владелец' if protected else role.name}",
        f"Статус: {'активен' if employee.is_active else 'отключён'}",
    ]
    rows: Buttons = []
    if not protected:
        role_key = "executor" if role.name == ROLE_KEYS["operator"] else "operator"
        role_text = "Сделать исполнителем" if role_key == "executor" else "Сделать оператором"
        rows.append([button(role_text, f"admin_staff_role:{employee.id}:{role_key}")])
        rows.append(
            [
                button(
                    "Отключить" if employee.is_active else "Включить",
                    f"admin_staff_active:{employee.id}:{0 if employee.is_active else 1}",
                )
            ]
        )
    rows.append([button("К сотрудникам", "admin_staff:0"), button("Управление", "admin")])
    await reply(db, actor, "\n".join(lines), rows)


async def _chat_card(
    db: AsyncSession,
    actor: Actor,
    organization_id: UUID,
    chat_id: int,
) -> None:
    chat = await db.get(BotGroupChat, chat_id, populate_existing=True)
    if chat is None or chat.organization_id != organization_id:
        raise CRMInvalidReference
    await reply(
        db,
        actor,
        "\n".join(
            [
                f"Групповой чат {chat.chat_id}",
                f"Анализ: {'включён' if chat.analysis_enabled else 'выключен'}",
                f"Последняя активность: {chat.last_seen_at:%d.%m.%Y %H:%M}",
            ]
        ),
        [
            [
                button(
                    "Выключить анализ" if chat.analysis_enabled else "Включить анализ",
                    f"admin_chat_toggle:{chat.chat_id}:{0 if chat.analysis_enabled else 1}",
                )
            ],
            [button("К чатам", "admin_chats:0"), button("Управление", "admin")],
        ],
    )


async def handle_owner_admin(
    db: AsyncSession,
    settings: Settings,
    update: IncomingUpdate,
    actor: Actor,
    state: BotConversation,
    organization_id: UUID,
    payload: str,
    text: str,
) -> bool:
    parts = payload.split(":") if payload else [""]
    action = parts[0]
    admin_state = state.state.startswith("admin_")
    if action not in ADMIN_ACTIONS and not admin_state:
        return False
    require_owner(actor)

    if action == "admin":
        state.state, state.data = "menu", {}
        await show_owner_admin(db, actor)
        return True

    if action == "admin_staff" and len(parts) == 2:
        state.state, state.data = "menu", {}
        offset = int(parts[1])
        rows = await list_staff(db, organization_id, offset=offset, limit=PAGE_SIZE + 1)
        buttons: Buttons = [
            [
                button(
                    ("✅ " if employee.is_active else "⛔ ")
                    + employee.display_name
                    + " · "
                    + ("Владелец" if employee.max_user_id in settings.max_owner_ids else role.name),
                    f"admin_staff_card:{employee.id}",
                )
            ]
            for employee, role in rows[:PAGE_SIZE]
        ]
        if len(rows) > PAGE_SIZE:
            buttons.append([button("Дальше", f"admin_staff:{offset + PAGE_SIZE}")])
        buttons.append([button("Добавить сотрудника", "admin_staff_add")])
        buttons.append([button("Управление", "admin")])
        await reply(db, actor, "Сотрудники" if rows else "Сотрудников пока нет.", buttons)
        return True

    if action == "admin_staff_card" and len(parts) == 2:
        state.state, state.data = "menu", {}
        await _staff_card(db, settings, actor, organization_id, UUID(parts[1]))
        return True

    if action == "admin_staff_add":
        state.state = "admin_staff_add_id"
        state.data = {}
        await reply(
            db,
            actor,
            "Пришлите MAX ID сотрудника одним сообщением.",
            [[button("Отмена", "admin_staff:0")]],
        )
        return True

    if state.state == "admin_staff_add_id" and text and not text.startswith("/"):
        if not text.isascii() or not text.isdecimal():
            raise CRMInvalidReference
        max_user_id = int(text)
        if not 0 < max_user_id < 2**63 or max_user_id in settings.max_owner_ids:
            raise CRMInvalidReference
        state.state = "admin_staff_add_role"
        state.data = {"max_user_id": str(max_user_id)}
        await reply(
            db,
            actor,
            f"MAX ID: {max_user_id}\nКакую роль выдать?",
            [
                [
                    button("Исполнитель", "admin_staff_add_role:executor"),
                    button("Оператор", "admin_staff_add_role:operator"),
                ],
                [button("Отмена", "admin_staff:0")],
            ],
        )
        return True

    if action == "admin_staff_add_role" and len(parts) == 2:
        if state.state != "admin_staff_add_role" or "max_user_id" not in state.data:
            raise CRMInvalidReference
        employee = await add_staff(
            db,
            actor,
            organization_id,
            int(state.data["max_user_id"]),
            parts[1],
        )
        state.state, state.data = "menu", {}
        await _staff_card(db, settings, actor, organization_id, employee.id)
        return True

    if action == "admin_staff_role" and len(parts) == 3:
        employee = await set_staff_role(
            db,
            actor,
            organization_id,
            UUID(parts[1]),
            parts[2],
            settings.max_owner_ids,
        )
        await _staff_card(db, settings, actor, organization_id, employee.id)
        return True

    if action == "admin_staff_active" and len(parts) == 3:
        if parts[2] not in {"0", "1"}:
            raise CRMInvalidReference
        employee = await set_staff_active(
            db,
            actor,
            organization_id,
            UUID(parts[1]),
            parts[2] == "1",
            settings.max_owner_ids,
        )
        await _staff_card(db, settings, actor, organization_id, employee.id)
        return True

    if action == "admin_roles":
        state.state, state.data = "menu", {}
        await reply(
            db,
            actor,
            "\n".join(
                [
                    "Роли",
                    "",
                    "Исполнитель:",
                    "• видит только свои задания",
                    "• меняет ход выполнения своих задач",
                    "",
                    "Оператор:",
                    "• видит диспетчерские очереди",
                    "• создаёт и назначает заявки",
                    "• получает сигналы из групповых чатов",
                    "",
                    "Владелец:",
                    "• все права оператора",
                    "• управление сотрудниками, чатами и настройками",
                ]
            ),
            [[button("Управление", "admin")]],
        )
        return True

    if action == "admin_chats" and len(parts) == 2:
        state.state, state.data = "menu", {}
        offset = int(parts[1])
        chats = await list_group_chats(db, organization_id, offset=offset, limit=PAGE_SIZE + 1)
        buttons = [
            [
                button(
                    ("✅ " if chat.analysis_enabled else "⛔ ") + f"Чат {chat.chat_id}",
                    f"admin_chat:{chat.chat_id}",
                )
            ]
            for chat in chats[:PAGE_SIZE]
        ]
        if len(chats) > PAGE_SIZE:
            buttons.append([button("Дальше", f"admin_chats:{offset + PAGE_SIZE}")])
        buttons.append([button("Управление", "admin")])
        await reply(
            db,
            actor,
            "Групповые чаты, в которых бот уже видел сообщения."
            if chats
            else "Бот ещё не видел сообщений в групповых чатах.",
            buttons,
        )
        return True

    if action == "admin_chat" and len(parts) == 2:
        state.state, state.data = "menu", {}
        await _chat_card(db, actor, organization_id, int(parts[1]))
        return True

    if action == "admin_chat_toggle" and len(parts) == 3:
        if parts[2] not in {"0", "1"}:
            raise CRMInvalidReference
        chat = await set_group_chat_analysis(
            db,
            actor,
            organization_id,
            int(parts[1]),
            parts[2] == "1",
        )
        await _chat_card(db, actor, organization_id, chat.chat_id)
        return True

    if action == "admin_settings":
        state.state, state.data = "menu", {}
        bot_settings = await get_bot_settings(db, organization_id)
        await reply(
            db,
            actor,
            "\n".join(
                [
                    "Настройки",
                    (
                        "Анализ групповых чатов: "
                        + ("включён" if bot_settings.group_analysis_enabled else "выключен")
                    ),
                    "Фильтр сообщений: DeepSeek V4 Flash",
                    (
                        "DeepSeek API: "
                        + ("настроен" if settings.deepseek_api_key else "не настроен")
                    ),
                    f"Модель: {settings.deepseek_model}",
                    "Thinking: включён · high",
                    f"MAX bot token: {'настроен' if settings.max_staff_token else 'не настроен'}",
                    (
                        "Webhook secret: "
                        + ("настроен" if settings.max_staff_webhook_secret else "не настроен")
                    ),
                    "",
                    "Токены и секреты меняются только на сервере и здесь не показываются.",
                ]
            ),
            [
                [
                    button(
                        "Выключить анализ всех чатов"
                        if bot_settings.group_analysis_enabled
                        else "Включить анализ всех чатов",
                        "admin_settings_group:"
                        + ("0" if bot_settings.group_analysis_enabled else "1"),
                    )
                ],
                [button("Управление", "admin")],
            ],
        )
        return True

    if action == "admin_settings_group" and len(parts) == 2:
        if parts[1] not in {"0", "1"}:
            raise CRMInvalidReference
        await set_global_group_analysis(
            db,
            actor,
            organization_id,
            parts[1] == "1",
        )
        await handle_owner_admin(
            db,
            settings,
            update,
            actor,
            state,
            organization_id,
            "admin_settings",
            "",
        )
        return True

    return admin_state
