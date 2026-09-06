# app/scheduler_utils.py
#
# Centralitza la lògica d'execució per hora fixa (diària) per als
# mòduls del scheduler que ho necessitin (sources, entries_rss).
#
# No substitueix ni duplica la funció is_due() existent a main.py
# (comprovació per interval de minuts). Aquest fitxer només afegeix
# is_due_daily_at, la variant per a execució diària a hora fixa
# (p.ex. "06:00", "09:00"), pensada per a config_map["crawler_runtime"]
# i config_map["entry_crawler_runtime"].

from datetime import datetime, timezone, time as time_cls
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Madrid")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_config_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    Parseja una data ISO guardada a public.config.
    Retorna None si el valor és buit o invàlid.

    Duplicat intencionadament de la funció equivalent a main.py per no
    crear un import creuat entre mòduls; és una funció pura sense
    efectes secundaris.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def is_due_daily_at(
    last_run_at: Optional[str],
    scheduled_time_str: str,
    force: bool = False,
) -> Tuple[bool, str]:
    """
    Determina si una tasca amb execució diària a hora fixa toca
    executar-se ara mateix.

    Paràmetres:
    - last_run_at: darrer timestamp ISO d'execució (pot ser None).
    - scheduled_time_str: hora local configurada, format "HH:MM"
      (p.ex. "06:00"). Es interpreta en zona horària Europe/Madrid.
    - force: si True, ignora tota comprovació i retorna due=True.

    Retorna (due, reason):
    - (True,  "forced")
        force=True; s'ignora la resta de lògica.
    - (True,  "never_run")
        Mai s'ha executat i ja ha passat l'hora programada d'avui.
    - (True,  "scheduled_window_open")
        L'última execució és d'un dia anterior i ja ha passat l'hora
        programada d'avui.
    - (False, "before_scheduled_time")
        Encara no ha arribat l'hora configurada d'avui.
    - (False, "already_run_today")
        Ja s'ha executat avui, dins de la finestra vàlida.
    - (True,  "invalid_runtime_fallback_never_run")
        scheduled_time_str no és parsejable (format invàlid). Fallback
        segur: es tracta com si toqués executar-se, per no deixar el
        mòdul bloquejat per una mala configuració.
    """
    if force:
        return True, "forced"

    try:
        hour_str, minute_str = scheduled_time_str.strip().split(":")
        scheduled_hour = int(hour_str)
        scheduled_minute = int(minute_str)
    except (ValueError, AttributeError):
        return True, "invalid_runtime_fallback_never_run"

    now_utc = _utc_now()
    now_local = now_utc.astimezone(LOCAL_TZ)
    scheduled_today = datetime.combine(
        now_local.date(),
        time_cls(scheduled_hour, scheduled_minute),
        tzinfo=LOCAL_TZ,
    )

    if now_local < scheduled_today:
        return False, "before_scheduled_time"

    last_run = _parse_config_datetime(last_run_at)
    if last_run is None:
        return True, "never_run"

    last_run_local = last_run.astimezone(LOCAL_TZ)
    if last_run_local.date() < now_local.date():
        return True, "scheduled_window_open"

    return False, "already_run_today"
