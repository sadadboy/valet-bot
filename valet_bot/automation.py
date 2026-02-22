from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any

from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright

BOOKING_URL = "https://valet.amanopark.co.kr/booking#/main"
BOOKING_LIST_URL = "https://valet.amanopark.co.kr/booking-list"


def _log_debug(log_path: Path, message: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def _dump_nav_state(page, log_path: Path, tag: str) -> None:
    try:
        payload = page.evaluate(
            """() => ({
                href: location.href,
                ready: document.readyState,
                historyLength: history.length,
                title: document.title,
                navEvents: window.__navEvents || []
            })"""
        )
        _log_debug(
            log_path,
            f"{tag} href={payload.get('href')} ready={payload.get('ready')} "
            f"history={payload.get('historyLength')} title={payload.get('title')} "
            f"events={payload.get('navEvents')}",
        )
    except Exception as exc:
        _log_debug(log_path, f"{tag} nav_state_failed: {exc}")


def _is_valid_booking_url(page) -> bool:
    try:
        u = page.url or ""
    except Exception:
        return False
    if u.startswith("about:blank"):
        return False
    return "valet.amanopark.co.kr/booking" in u


def _select_by_label_or_fallback(page, label: str, value: str, fallback_index: int | None = None) -> bool:
    # 1) Try explicit label bindings first.
    try:
        sel = page.get_by_label(label, exact=False)
        if sel.count() > 0:
            sel.first.select_option(label=value, timeout=1200)
            selected = sel.first.input_value()
            if selected:
                return True
    except Exception:
        pass

    # 2) Try native select near the visible label text.
    try:
        near = page.locator(f"label:has-text('{label}')").first.locator("xpath=following::select[1]")
        if near.count() > 0:
            near.first.select_option(label=value, timeout=1200)
            if near.first.input_value():
                return True
    except Exception:
        pass

    # 3) Fallback to known positional index if provided.
    if fallback_index is not None:
        try:
            page.locator("select").nth(fallback_index).select_option(label=value, timeout=1200)
            selected = page.locator("select").nth(fallback_index).input_value()
            if selected:
                return True
        except Exception:
            pass

    return False


def _select_by_option_text(page, value: str) -> bool:
    # Scan all native selects and pick the first one that has the target option text.
    selects = page.locator("select")
    count = selects.count()
    for i in range(count):
        sel = selects.nth(i)
        try:
            options = sel.locator("option")
            for j in range(options.count()):
                txt = options.nth(j).inner_text(timeout=500).strip()
                if txt == value:
                    sel.select_option(label=value, timeout=1200)
                    return True
        except Exception:
            continue
    return False


def _select_custom_dropdown_by_label(page, label: str, value: str) -> bool:
    # For custom dropdown UIs (not native <select>), click the control next to label and choose text option.
    patterns = [
        f"xpath=//*[contains(normalize-space(.), '{label}')]/following::*[self::div or self::button or self::input][contains(@class,'select') or contains(@class,'dropdown') or @role='combobox'][1]",
        f"xpath=//label[contains(normalize-space(.), '{label}')]/following::*[self::div or self::button or self::input][1]",
    ]
    for p in patterns:
        try:
            control = page.locator(p).first
            if control.count() == 0:
                continue
            control.click(timeout=1200)
            opt = page.locator(
                f"li:has-text('{value}'), .dropdown-menu *:has-text('{value}'), .select2-results__option:has-text('{value}')"
            ).first
            if opt.count() == 0:
                page.keyboard.press("Escape")
                continue
            opt.click(timeout=1200)
            return True
        except Exception:
            continue
    return False


def _force_set_select_by_option_text(page, value: str) -> bool:
    try:
        ok = page.evaluate(
            """(targetText) => {
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const wanted = norm(targetText);
                const selects = Array.from(document.querySelectorAll("select"));
                for (const sel of selects) {
                    const options = Array.from(sel.options || []);
                    const matched = options.find(opt => norm(opt.textContent) === wanted);
                    if (!matched) continue;
                    sel.value = matched.value;
                    sel.dispatchEvent(new Event("input", { bubbles: true }));
                    sel.dispatchEvent(new Event("change", { bubbles: true }));
                    return true;
                }
                return false;
            }""",
            value,
        )
        return bool(ok)
    except Exception:
        return False


def _field_already_has_value(page, label: str, value: str) -> bool:
    # If UI already shows the desired value (e.g., default "일반"), treat as applied.
    try:
        found = page.evaluate(
            """(args) => {
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const label = norm(args.label);
                const value = norm(args.value);
                const labels = Array.from(document.querySelectorAll("label, div, span, p"));
                for (const el of labels) {
                    if (!norm(el.textContent).includes(label)) continue;
                    const row = el.closest(".form-group, .row, .col-md-6, .col-sm-6, .col-6, .field, li, section, article") || el.parentElement;
                    if (!row) continue;
                    if (norm(row.textContent).includes(value)) return true;
                }
                return false;
            }""",
            {"label": label, "value": value},
        )
        return bool(found)
    except Exception:
        return False


def _fill_text_fields(page, booking: dict[str, Any]) -> None:
    page.get_by_placeholder("한글 또는 영문으로 입력해 주세요.").fill(booking["name"])
    page.get_by_placeholder("'-'없이 숫자만 입력해 주세요.").fill(booking["phone"])
    page.get_by_placeholder("차량번호를 입력해 주세요.").fill(booking["car_number"])
    page.get_by_placeholder("차량모델을 정확히 입력해 주세요.").fill(booking["car_model"])


def _pick_day_in_calendar(page, target_date: str, input_index: int) -> tuple[bool, str]:
    # input_index: 0 for departure, 1 for arrival
    year, month, day = [int(x) for x in target_date.split("-")]
    date_inputs = page.locator("input[placeholder*='년도']:visible")
    if date_inputs.count() <= input_index:
        return False, "date_input_not_found"

    opened = _open_calendar_popup(page, input_index)
    # Do not fail early: some calendars are visible but not detectable by selectors.
    if not opened:
        page.wait_for_timeout(250)

    # Element UI date picker (el-picker-panel el-date-picker) path.
    if page.locator(".el-picker-panel.el-date-picker:visible").count() > 0:
        return _pick_day_in_element_ui(page, target_date, input_index)

    month_title = page.locator(
        ".datepicker-switch:visible, "
        ".b-calendar .b-calendar-nav .form-control:visible, "
        ".b-calendar .b-calendar-nav .btn[aria-live='polite']:visible"
    )
    for _ in range(36):
        try:
            title = month_title.first.inner_text(timeout=2000).strip()
            parsed = _parse_calendar_title(title)
            if parsed is None:
                break
            cur_year, cur_month = parsed
            if cur_year == year and cur_month == month:
                break
            if (cur_year, cur_month) < (year, month):
                moved = _click_calendar_nav(page, "next")
                if not moved:
                    return False, "calendar_next_click_failed"
            else:
                moved = _click_calendar_nav(page, "prev")
                if not moved:
                    return False, "calendar_prev_click_failed"
            page.wait_for_timeout(150)
            continue
        except Exception:
            break

    enabled_cell = page.locator(
        f"td.day:not(.disabled):visible:text-is('{day}'), "
        f".datepicker-days td:not(.disabled):visible:text-is('{day}'), "
        f".b-calendar .b-calendar-grid-body .btn:not([disabled]):visible:text-is('{day}')"
    ).first
    disabled_cell = page.locator(
        f"td.day.disabled:visible:text-is('{day}'), "
        f".datepicker-days td.disabled:visible:text-is('{day}'), "
        f".b-calendar .b-calendar-grid-body .btn[disabled]:visible:text-is('{day}')"
    ).first
    any_cell = page.locator(
        f"td.day:visible:text-is('{day}'), "
        f".datepicker-days td:visible:text-is('{day}'), "
        f".b-calendar .b-calendar-grid-body .btn:visible:text-is('{day}')"
    ).first

    if enabled_cell.count() == 0:
        if disabled_cell.count() > 0:
            return False, "target_date_disabled"
        if any_cell.count() > 0:
            return False, "target_date_present_but_not_clickable"
        return False, "target_date_not_present_open_unknown" if not opened else "target_date_not_present"
    try:
        enabled_cell.click(timeout=2000)
    except Exception:
        return False, "target_date_click_failed"
    page.wait_for_timeout(150)

    value = date_inputs.nth(input_index).input_value().strip()
    if str(year) in value and f"{month:02d}" in value and f"{day:02d}" in value:
        return True, "selected"

    return False, "selected_value_not_applied"


def _pick_day_in_element_ui(page, target_date: str, input_index: int) -> tuple[bool, str]:
    year, month, day = [int(x) for x in target_date.split("-")]
    panel = page.locator(".el-picker-panel.el-date-picker:visible").first
    date_inputs = page.locator("input[placeholder*='년도']:visible")
    if panel.count() == 0:
        return False, "element_panel_not_found"

    for _ in range(36):
        ym = _element_ui_current_year_month(page)
        if ym is None:
            break
        cur_year, cur_month = ym
        if (cur_year, cur_month) == (year, month):
            break
        if (cur_year, cur_month) < (year, month):
            if not _click_element_ui_nav(page, "next"):
                return False, "element_nav_next_failed"
        else:
            if not _click_element_ui_nav(page, "prev"):
                return False, "element_nav_prev_failed"
        page.wait_for_timeout(180)

    cell = panel.locator(
        f".el-date-table td.available:not(.disabled):not(.prev-month):not(.next-month) span:text-is('{day}')"
    ).first
    if cell.count() == 0:
        disabled = panel.locator(
            f".el-date-table td.disabled span:text-is('{day}'), "
            f".el-date-table td.prev-month span:text-is('{day}'), "
            f".el-date-table td.next-month span:text-is('{day}')"
        ).first
        if disabled.count() > 0:
            return False, "element_target_day_disabled"
        return False, "element_target_day_not_found"

    try:
        cell.click(timeout=1500, force=True)
    except Exception:
        return False, "element_target_day_click_failed"

    if date_inputs.count() > input_index:
        value = date_inputs.nth(input_index).input_value().strip()
        if str(year) in value and f"{month:02d}" in value and f"{day:02d}" in value:
            return True, "selected_element"
    return True, "selected_element_unverified"


def _element_ui_current_year_month(page) -> tuple[int, int] | None:
    try:
        labels = page.locator(".el-picker-panel.el-date-picker:visible .el-date-picker__header-label")
        text = " ".join([t.strip() for t in labels.all_inner_texts() if t.strip()])
        if not text:
            text = page.locator(".el-picker-panel.el-date-picker:visible").first.inner_text(timeout=1000)
        return _parse_calendar_title(text)
    except Exception:
        return None


def _click_element_ui_nav(page, direction: str) -> bool:
    if direction == "next":
        selectors = [
            ".el-picker-panel.el-date-picker:visible .el-icon-arrow-right",
            ".el-picker-panel.el-date-picker:visible .el-icon-d-arrow-right",
        ]
    else:
        selectors = [
            ".el-picker-panel.el-date-picker:visible .el-icon-arrow-left",
            ".el-picker-panel.el-date-picker:visible .el-icon-d-arrow-left",
        ]
    for s in selectors:
        try:
            btn = page.locator(s).first
            if btn.count() == 0:
                continue
            btn.click(timeout=1200, force=True)
            return True
        except Exception:
            continue
    return False


def _open_calendar_popup(page, input_index: int) -> bool:
    date_inputs = page.locator("input[placeholder*='년도']:visible")
    if date_inputs.count() <= input_index:
        return False
    target = date_inputs.nth(input_index)
    try:
        target.click(timeout=1200, force=True)
        page.wait_for_timeout(300)
    except Exception:
        pass

    if _has_visible_calendar(page):
        return True

    # Try JS focus/click events directly on the input.
    try:
        ok = page.evaluate(
            """(idx) => {
                const inputs = Array.from(document.querySelectorAll("input[placeholder*='년도'], input[placeholder*='년']"));
                const visible = inputs.filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const t = visible[idx] || inputs[idx];
                if (!t) return false;
                t.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                t.focus();
                t.click();
                t.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                t.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                return true;
            }""",
            input_index,
        )
        if ok:
            page.wait_for_timeout(300)
    except Exception:
        pass

    if _has_visible_calendar(page):
        return True

    # Fallback: click the calendar icon/addon next to the date input.
    try:
        icon = target.locator(
            "xpath=following::*[self::span or self::i or self::button]"
            "[contains(@class,'calendar') or contains(@class,'glyphicon') or contains(@class,'fa')][1]"
        )
        if icon.count() > 0:
            icon.first.click(timeout=1200, force=True)
            page.wait_for_timeout(300)
    except Exception:
        pass

    if _has_visible_calendar(page):
        return True

    # jQuery/bootstrap datepicker fallback.
    try:
        opened = page.evaluate(
            """(idx) => {
                const w = window;
                const jq = w.jQuery || w.$;
                if (!jq) return false;
                const $inputs = jq("input[placeholder*='년도'], input[placeholder*='년']");
                const el = $inputs.get(idx);
                if (!el) return false;
                try {
                    jq(el).datepicker('show');
                    return true;
                } catch (e) {
                    return false;
                }
            }""",
            input_index,
        )
        if opened:
            page.wait_for_timeout(300)
    except Exception:
        pass

    if _has_visible_calendar(page):
        return True

    # Last fallback: click near input container.
    try:
        target.locator("xpath=ancestor::div[1]").click(timeout=800, force=True)
        page.wait_for_timeout(250)
    except Exception:
        pass
    return _has_visible_calendar(page)


def _has_visible_calendar(page) -> bool:
    try:
        cnt = page.locator(
            ".el-picker-panel.el-date-picker:visible, "
            ".datepicker-switch:visible, "
            "td.day:visible, "
            ".datepicker-days td:visible, "
            ".b-calendar .b-calendar-grid-body .btn:visible"
        ).count()
        return cnt > 0
    except Exception:
        return False


def _click_calendar_nav(page, direction: str) -> bool:
    if direction not in ("next", "prev"):
        return False
    if direction == "next":
        selectors = [
            ".el-picker-panel.el-date-picker:visible .el-icon-arrow-right",
            ".el-picker-panel.el-date-picker:visible .el-icon-d-arrow-right",
            "th.next:visible",
            ".datepicker-days th.next:visible",
            ".next:visible",
            "th:visible:text-is('›')",
            "th:visible:text-is('»')",
            ".b-calendar .b-calendar-nav .next",
            ".b-calendar .b-calendar-nav .btn[aria-label*='Next']",
            ".b-calendar .b-calendar-nav .btn[title*='Next']",
        ]
    else:
        selectors = [
            ".el-picker-panel.el-date-picker:visible .el-icon-arrow-left",
            ".el-picker-panel.el-date-picker:visible .el-icon-d-arrow-left",
            "th.prev:visible",
            ".datepicker-days th.prev:visible",
            ".prev:visible",
            "th:visible:text-is('‹')",
            "th:visible:text-is('«')",
            ".b-calendar .b-calendar-nav .prev",
            ".b-calendar .b-calendar-nav .btn[aria-label*='Previous']",
            ".b-calendar .b-calendar-nav .btn[title*='Previous']",
        ]

    for s in selectors:
        try:
            loc = page.locator(s).first
            if loc.count() > 0:
                loc.click(timeout=1200, force=True)
                return True
        except Exception:
            continue

    try:
        clicked = page.evaluate(
            """(dir) => {
                const cands = dir === 'next'
                  ? [
                    'th.next', '.datepicker-days th.next', '.next',
                    "th",
                    '.b-calendar .b-calendar-nav .next',
                    '.b-calendar .b-calendar-nav .btn[aria-label*="Next"]',
                    '.b-calendar .b-calendar-nav .btn[title*="Next"]'
                  ]
                  : [
                    'th.prev', '.datepicker-days th.prev', '.prev',
                    "th",
                    '.b-calendar .b-calendar-nav .prev',
                    '.b-calendar .b-calendar-nav .btn[aria-label*="Previous"]',
                    '.b-calendar .b-calendar-nav .btn[title*="Previous"]'
                  ];
                for (const sel of cands) {
                  const els = Array.from(document.querySelectorAll(sel));
                  const el = els.find(e => {
                    if (!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
                    const t = (e.textContent || '').trim();
                    if (sel === 'th') {
                      return dir === 'next' ? (t === '›' || t === '»') : (t === '‹' || t === '«');
                    }
                    return true;
                  });
                  if (!el) continue;
                  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                  el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                  el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                  return true;
                }
                return false;
            }""",
            direction,
        )
        return bool(clicked)
    except Exception:
        return False


def _calendar_debug_snapshot(page) -> str:
    try:
        return page.evaluate(
            """() => {
                const titleEl =
                  document.querySelector('.datepicker-switch')
                  || document.querySelector('.b-calendar .b-calendar-nav .form-control')
                  || document.querySelector('.b-calendar .b-calendar-nav .btn[aria-live="polite"]');
                const title = titleEl?.textContent?.trim() || 'no_title';
                const days = Array.from(document.querySelectorAll('td.day'))
                  .map(td => `${td.textContent?.trim()}:${td.className}`)
                  .slice(0, 80);
                const bdays = Array.from(document.querySelectorAll('.b-calendar .b-calendar-grid-body .btn'))
                  .map(td => `${td.textContent?.trim()}:${td.className}`)
                  .slice(0, 80);
                if (!days.length && !bdays.length) {
                  const visible = Array.from(document.querySelectorAll('body *'))
                    .filter(el => {
                      const st = window.getComputedStyle(el);
                      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                      return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    });
                  const monthLike = visible
                    .filter(el => /(January|February|March|April|May|June|July|August|September|October|November|December|\\d{4}\\s+[A-Za-z]+|[0-9]{4}년\\s*[0-9]{1,2}월)/i.test((el.textContent||'').trim()))
                    .slice(0, 10)
                    .map(el => `${el.tagName}.${el.className}::${(el.textContent||'').trim().slice(0,60)}`);
                  const dayLike = visible
                    .filter(el => {
                      const t = (el.textContent||'').trim();
                      return /^(?:[1-9]|[12][0-9]|3[01])$/.test(t);
                    })
                    .slice(0, 30)
                    .map(el => `${el.tagName}.${el.className}::${(el.textContent||'').trim()}`);
                  const navLike = visible
                    .filter(el => /(next|prev|previous|다음|이전|›|‹|»|«)/i.test((el.textContent||'').trim()))
                    .slice(0, 20)
                    .map(el => `${el.tagName}.${el.className}::${(el.textContent||'').trim().slice(0,20)}`);
                  return `picker_not_found title=${title} monthLike=${monthLike.join('||')} dayLike=${dayLike.join('||')} navLike=${navLike.join('||')}`;
                }
                return `title=${title} day_count=${days.length} bday_count=${bdays.length} days=${days.join('|')} bdays=${bdays.join('|')}`;
            }"""
        )
    except Exception as exc:
        return f"calendar_debug_error:{exc}"


def _month_to_number(name: str) -> int:
    mapping = {
        "January": 1,
        "February": 2,
        "March": 3,
        "April": 4,
        "May": 5,
        "June": 6,
        "July": 7,
        "August": 8,
        "September": 9,
        "October": 10,
        "November": 11,
        "December": 12,
    }
    if name.isdigit():
        n = int(name)
        if 1 <= n <= 12:
            return n
        return 0
    return mapping.get(name, 1)


def _parse_calendar_title(title: str) -> tuple[int, int] | None:
    # Handles examples: "2026 April", "April 2026", "2026년 4월"
    year_match = re.search(r"(20\d{2})", title)
    if not year_match:
        return None
    year = int(year_match.group(1))

    month_match_num = re.search(r"(1[0-2]|0?[1-9])\s*월", title)
    if month_match_num:
        return year, int(month_match_num.group(1))

    month_names = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    low = title.lower()
    for name, num in month_names.items():
        if name in low:
            return year, num

    stripped = title.replace(str(year), " ")
    month_match_plain = re.search(r"\b(1[0-2]|0?[1-9])\b", stripped)
    if month_match_plain:
        return year, int(month_match_plain.group(1))

    return None


def _pick_time(page, value: str, input_index: int) -> bool:
    time_inputs = page.locator("input[value='00:00'], input[placeholder='00:00'], input[placeholder*='00:00']")
    if time_inputs.count() > input_index:
        try:
            time_inputs.nth(input_index).click()
            time_inputs.nth(input_index).fill(value)
            page.locator("body").click()
            return True
        except Exception:
            pass

    # Custom time dropdown: click nearby trigger and choose target time text.
    try:
        date_inputs = page.locator("input[placeholder*='년도']:visible")
        if date_inputs.count() > input_index:
            date_inputs.nth(input_index).locator("xpath=following::input[1]").click(timeout=1200)
            opt = page.locator(
                f"li:has-text('{value}'), .dropdown-menu *:has-text('{value}'), .timepicker *:has-text('{value}')"
            ).first
            if opt.count() > 0:
                opt.click(timeout=1200)
                return True
    except Exception:
        pass

    # JS fallback for readonly/custom inputs.
    try:
        ok = page.evaluate(
            """(args) => {
                const { index, value } = args;
                const inputs = Array.from(document.querySelectorAll("input"));
                const candidates = inputs.filter(el => {
                  const p = (el.getAttribute("placeholder") || "").trim();
                  const v = (el.value || "").trim();
                  return p === "00:00" || v === "00:00" || /\\d{2}:\\d{2}/.test(v);
                });
                const t = candidates[index];
                if (!t) return false;
                t.removeAttribute('readonly');
                t.value = value;
                t.dispatchEvent(new Event('input', { bubbles: true }));
                t.dispatchEvent(new Event('change', { bubbles: true }));
                t.dispatchEvent(new Event('blur', { bubbles: true }));
                return true;
            }""",
            {"index": input_index, "value": value},
        )
        return bool(ok)
    except Exception:
        pass

    try:
        page.locator("select").filter(has_text="00:00").nth(input_index).select_option(label=value)
        return True
    except Exception:
        return False


def _check_all_checkboxes(page) -> None:
    # Required consent-only flow. Do not touch optional top checkboxes.
    targets = [
        "약관의 내용을 모두 확인하였으며, 동의합니다.",
        "프리미엄 서비스 요금 및 일반주차 요금을 확인하였습니다.",
        "세차 서비스에 대한 내용을 모두 확인하였습니다.",
        "위 약관에 모두 동의합니다.",
    ]
    for text in targets:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=800)
        except Exception:
            continue

    # Safety: force optional checkboxes off.
    try:
        page.evaluate(
            """() => {
                const labels = Array.from(document.querySelectorAll("label, span, div"));
                const turnOff = ["상주 직원", "세차 서비스"];
                for (const word of turnOff) {
                    const hit = labels.find(el => (el.textContent || "").includes(word));
                    if (!hit) continue;
                    const row = hit.closest(".row, .form-group, div") || hit.parentElement;
                    if (!row) continue;
                    const cb = row.querySelector("input[type='checkbox']");
                    if (!cb) continue;
                    if (cb.checked) {
                        cb.checked = false;
                        cb.dispatchEvent(new Event('input', { bubbles: true }));
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            }"""
        )
    except Exception:
        pass


def _checkbox_stats(page) -> tuple[int, int]:
    try:
        total, checked = page.evaluate(
            """() => {
                const boxes = Array.from(document.querySelectorAll("input[type='checkbox']"));
                return [boxes.length, boxes.filter(b => b.checked).length];
            }"""
        )
        return int(total), int(checked)
    except Exception:
        return 0, 0


def _detect_success(page, booking: dict[str, Any]) -> tuple[bool, str]:
    # Primary signal: redirected to booking-list page after final confirm.
    try:
        if "/booking-list" in (page.url or ""):
            body_text = page.inner_text("body")
            matched = []
            name = str(booking.get("name", "")).strip()
            phone = str(booking.get("phone", "")).strip()
            car_number = str(booking.get("car_number", "")).strip()
            if name and name in body_text:
                matched.append("name")
            if car_number and car_number in body_text:
                matched.append("car_number")
            if phone and phone in body_text:
                matched.append("phone")
            reservation_id = _extract_reservation_id(body_text)
            if matched:
                return True, f"success_by_url_and_profile_match:{','.join(matched)};reservation_id={reservation_id or '-'}"
            return True, f"success_by_url_only:booking-list;reservation_id={reservation_id or '-'}"
    except Exception:
        pass

    body = page.content()
    success_keywords = ["예약이 완료", "예약 완료", "등록되었습니다", "예약번호", "접수번호"]
    for keyword in success_keywords:
        if keyword in body:
            return True, f"success_keyword:{keyword}"
    # Optional toast-like signal (ephemeral; best-effort only).
    toast_keywords = ["예약", "완료", "등록"]
    try:
        visible_text = page.inner_text("body")
        if all(k in visible_text for k in ["예약", "완료"]) or "등록" in visible_text:
            return False, "toast_like_signal_detected_but_unverified"
    except Exception:
        pass
    return False, "success_not_detected"


def _extract_reservation_id(text: str) -> str | None:
    patterns = [
        r"(예약번호)\s*[:：]?\s*([A-Za-z0-9\-]{4,})",
        r"(접수번호)\s*[:：]?\s*([A-Za-z0-9\-]{4,})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(2)
    return None


def _confirm_submit_modal(page) -> tuple[bool, str]:
    # Final confirmation modal: click "확인" button if present.
    candidates = [
        page.get_by_role("button", name="확인"),
        page.locator("button:has-text('확인')"),
        page.locator(".el-message-box__btns button:has-text('확인')"),
        page.locator(".modal button:has-text('확인')"),
    ]
    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            loc.first.click(timeout=1500, force=True)
            page.wait_for_timeout(1200)
            return True, "confirm_clicked"
        except Exception:
            continue
    return False, "confirm_not_found"


def run_booking_attempt(config: dict[str, Any], screenshot_dir: Path) -> dict[str, Any]:
    schedule = config["schedule"]
    booking = config["booking"]
    runtime = config["runtime"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot_path = screenshot_dir / f"attempt_{ts}.png"
    debug_enabled = bool(runtime.get("debug_enabled", True))
    debug_dir = screenshot_dir.parent / "data" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_log_path = debug_dir / f"attempt_{ts}.log"
    trace_path = debug_dir / f"attempt_{ts}.zip"

    result = {
        "ok": False,
        "status": "failed",
        "message": "unknown_error",
        "screenshot_path": str(shot_path),
        "debug_log_path": str(debug_log_path),
        "trace_path": str(trace_path) if debug_enabled else None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=runtime["headless"], slow_mo=runtime["slow_mo_ms"])
        context = browser.new_context(locale="ko-KR")
        if debug_enabled:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.set_default_timeout(runtime["timeout_ms"])
        if debug_enabled:
            page.add_init_script(
                """
                window.__navEvents = [];
                const push = history.pushState.bind(history);
                const replace = history.replaceState.bind(history);
                const back = history.back.bind(history);
                const go = history.go.bind(history);
                history.pushState = function(...args){ window.__navEvents.push("pushState"); return push(...args); };
                history.replaceState = function(...args){ window.__navEvents.push("replaceState"); return replace(...args); };
                history.back = function(...args){ window.__navEvents.push("history.back"); return back(...args); };
                history.go = function(...args){ window.__navEvents.push("history.go"); return go(...args); };
                window.addEventListener("popstate", () => window.__navEvents.push("popstate"));
                """
            )
            page.on("framenavigated", lambda frame: _log_debug(debug_log_path, f"framenavigated:{frame.url}"))
            page.on("domcontentloaded", lambda: _log_debug(debug_log_path, f"domcontentloaded:{page.url}"))
            page.on("load", lambda: _log_debug(debug_log_path, f"load:{page.url}"))
            page.on("pageerror", lambda e: _log_debug(debug_log_path, f"pageerror:{e}"))
            page.on("console", lambda msg: _log_debug(debug_log_path, f"console[{msg.type}]: {msg.text}"))
            page.on(
                "dialog",
                lambda d: (_log_debug(debug_log_path, f"dialog[{d.type}]: {d.message}"), d.accept()),
            )

        try:
            if debug_enabled:
                _log_debug(debug_log_path, f"start headless={runtime['headless']} timeout_ms={runtime['timeout_ms']}")
            page.goto(BOOKING_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            if debug_enabled:
                _dump_nav_state(page, debug_log_path, "after_goto")
            if "valet.amanopark.co.kr" not in page.url:
                if debug_enabled:
                    _log_debug(debug_log_path, f"unexpected_url_after_first_goto:{page.url}")
                page.goto(BOOKING_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                if debug_enabled:
                    _dump_nav_state(page, debug_log_path, "after_second_goto")
            if page.url.startswith("about:blank"):
                result["status"] = "navigation_failed"
                result["message"] = "navigated_to_blank_page"
                if debug_enabled:
                    _log_debug(debug_log_path, "navigation_failed:about_blank")
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            try:
                page.wait_for_selector("text=예약정보입력", timeout=6000)
                if debug_enabled:
                    _dump_nav_state(page, debug_log_path, "form_visible")
            except Exception:
                body_text = page.inner_text("body").strip()
                if not body_text:
                    result["status"] = "page_blank_or_blocked"
                    result["message"] = f"url={page.url}"
                else:
                    result["status"] = "page_not_ready"
                    result["message"] = f"url={page.url},body={body_text[:120]}"
                page.screenshot(path=str(shot_path), full_page=True)
                return result

            _fill_text_fields(page, booking)
            if debug_enabled:
                _dump_nav_state(page, debug_log_path, "after_fill_fields")
            if not _is_valid_booking_url(page):
                result["status"] = "navigation_lost"
                result["message"] = f"lost_after_fill:url={page.url}"
                page.screenshot(path=str(shot_path), full_page=True)
                return result

            service_ok = _select_by_label_or_fallback(page, "서비스 유형", booking["service_type"], fallback_index=0)
            if not service_ok:
                service_ok = _select_by_option_text(page, booking["service_type"])
            if not service_ok:
                service_ok = _select_custom_dropdown_by_label(page, "서비스 유형", booking["service_type"])
            if not service_ok:
                service_ok = _force_set_select_by_option_text(page, booking["service_type"])
            if not service_ok:
                service_ok = _field_already_has_value(page, "서비스 유형", booking["service_type"])
            if not _is_valid_booking_url(page):
                result["status"] = "navigation_lost"
                result["message"] = f"lost_after_service_select:url={page.url}"
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            brand_ok = _select_by_label_or_fallback(page, "차량 브랜드", booking["brand"], fallback_index=2)
            if not brand_ok:
                brand_ok = _select_by_option_text(page, booking["brand"])
            if not brand_ok:
                brand_ok = _select_custom_dropdown_by_label(page, "차량 브랜드", booking["brand"])
            if not brand_ok:
                brand_ok = _force_set_select_by_option_text(page, booking["brand"])
            if not _is_valid_booking_url(page):
                result["status"] = "navigation_lost"
                result["message"] = f"lost_after_brand_select:url={page.url}"
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            color_ok = _select_by_label_or_fallback(page, "색상", booking["color"], fallback_index=3)
            if not color_ok:
                color_ok = _select_by_option_text(page, booking["color"])
            if not color_ok:
                color_ok = _select_custom_dropdown_by_label(page, "색상", booking["color"])
            if not color_ok:
                color_ok = _force_set_select_by_option_text(page, booking["color"])
            if not _is_valid_booking_url(page):
                result["status"] = "navigation_lost"
                result["message"] = f"lost_after_color_select:url={page.url}"
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            discount_ok = _select_by_label_or_fallback(page, "할인 유형", booking["discount_type"], fallback_index=4)
            if not discount_ok:
                discount_ok = _select_by_option_text(page, booking["discount_type"])
            if not discount_ok:
                discount_ok = _select_custom_dropdown_by_label(page, "할인 유형", booking["discount_type"])
            if not discount_ok:
                discount_ok = _force_set_select_by_option_text(page, booking["discount_type"])
            if not discount_ok:
                discount_ok = _field_already_has_value(page, "할인 유형", booking["discount_type"])
            if not _is_valid_booking_url(page):
                result["status"] = "navigation_lost"
                result["message"] = f"lost_after_discount_select:url={page.url}"
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            if debug_enabled:
                _log_debug(
                    debug_log_path,
                    f"select_result service={service_ok} brand={brand_ok} color={color_ok} discount={discount_ok}",
                )

            dep_time_ok = True
            arr_time_ok = True
            if not runtime.get("test_skip_dates", False):
                depart_ok, depart_reason = _pick_day_in_calendar(
                    page, schedule["target_departure_date"], input_index=0
                )
                if debug_enabled:
                    _log_debug(debug_log_path, f"departure_pick ok={depart_ok} reason={depart_reason}")
                    _log_debug(debug_log_path, f"departure_title_probe { _calendar_debug_snapshot(page) }")
                    if not depart_ok:
                        _log_debug(debug_log_path, f"departure_calendar_snapshot { _calendar_debug_snapshot(page) }")
                if not depart_ok:
                    result["status"] = "date_not_open"
                    if depart_reason in ("target_date_click_failed", "selected_value_not_applied", "selected_value_apply_exception"):
                        result["status"] = "date_open_but_select_failed"
                    result["message"] = f"departure:{depart_reason}"
                    page.screenshot(path=str(shot_path), full_page=True)
                    return result

                arrive_ok, arrive_reason = _pick_day_in_calendar(
                    page, schedule["target_arrival_date"], input_index=1
                )
                if debug_enabled:
                    _log_debug(debug_log_path, f"arrival_pick ok={arrive_ok} reason={arrive_reason}")
                    if not arrive_ok:
                        _log_debug(debug_log_path, f"arrival_calendar_snapshot { _calendar_debug_snapshot(page) }")
                if not arrive_ok:
                    result["status"] = "invalid_arrival_date"
                    result["message"] = f"arrival:{arrive_reason}"
                    page.screenshot(path=str(shot_path), full_page=True)
                    return result

                dep_time_ok = _pick_time(page, schedule["departure_time"], input_index=0)
                arr_time_ok = _pick_time(page, schedule["arrival_time"], input_index=1)
                if debug_enabled:
                    _log_debug(debug_log_path, f"time_pick dep={dep_time_ok} arr={arr_time_ok}")
                if not dep_time_ok or not arr_time_ok:
                    result["status"] = "time_not_applied"
                    result["message"] = f"departure_time={dep_time_ok},arrival_time={arr_time_ok}"
                    page.screenshot(path=str(shot_path), full_page=True)
                    return result
            else:
                if debug_enabled:
                    _log_debug(debug_log_path, "test_skip_dates=true; skipped date/time steps")

            airline_ok = _select_by_label_or_fallback(page, "출발 항공편", booking["airline"], fallback_index=5)
            if not airline_ok:
                airline_ok = _select_by_option_text(page, booking["airline"])
            if not airline_ok:
                airline_ok = _select_custom_dropdown_by_label(page, "출발 항공편", booking["airline"])
            if not airline_ok:
                airline_ok = _force_set_select_by_option_text(page, booking["airline"])
            if debug_enabled:
                _log_debug(debug_log_path, f"airline_pick ok={airline_ok}")

            if not service_ok or not discount_ok or not airline_ok:
                result["status"] = "select_not_applied"
                result["message"] = (
                    f"service={service_ok},brand={brand_ok},color={color_ok},discount={discount_ok},"
                    f"airline={airline_ok},dep_time={dep_time_ok},arr_time={arr_time_ok}"
                )
                page.screenshot(path=str(shot_path), full_page=True)
                return result

            _check_all_checkboxes(page)
            if debug_enabled:
                total_cb, checked_cb = _checkbox_stats(page)
                _log_debug(debug_log_path, f"checkbox_stats checked={checked_cb}/{total_cb}")
                _dump_nav_state(page, debug_log_path, "after_checkbox")

            page.get_by_role("button", name="등록하기").click()
            page.wait_for_timeout(2500)
            if debug_enabled:
                _dump_nav_state(page, debug_log_path, "after_submit_click")

            confirm_ok, confirm_msg = _confirm_submit_modal(page)
            if debug_enabled:
                _log_debug(debug_log_path, f"submit_confirm ok={confirm_ok} msg={confirm_msg}")
            try:
                page.wait_for_url("**/booking-list**", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1000)

            ok, message = _detect_success(page, booking)
            result["ok"] = ok
            result["status"] = "success" if ok else "submitted_but_unconfirmed"
            result["message"] = f"{message};{confirm_msg}"
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        except PWTimeoutError as exc:
            result["status"] = "timeout"
            result["message"] = str(exc)
            if debug_enabled:
                _log_debug(debug_log_path, f"timeout:{exc}")
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        except Exception as exc:
            result["status"] = "exception"
            result["message"] = str(exc)
            if debug_enabled:
                _log_debug(debug_log_path, f"exception:{exc}")
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        finally:
            if debug_enabled:
                try:
                    context.tracing.stop(path=str(trace_path))
                except Exception as exc:
                    _log_debug(debug_log_path, f"trace_stop_failed:{exc}")
            context.close()
            browser.close()


def _fill_booking_lookup(page, car_number: str, phone: str) -> None:
    normalized_phone = re.sub(r"\D", "", phone or "")
    _apply_lookup_inputs(page, car_number, normalized_phone)
    _click_lookup_confirm(page)
    _wait_booking_list_rows(page, timeout_ms=5000)


def _apply_lookup_inputs(page, car_number: str, phone: str) -> None:
    car_ok = _fill_input_near_label(
        page,
        label_patterns=["차량번호"],
        value=car_number,
    )
    phone_ok = _fill_input_near_label(
        page,
        label_patterns=["휴대전화", "전화번호", "휴대폰"],
        value=phone,
    )

    # Fallback: use visible text inputs order (car first, phone second).
    visible_text_inputs = page.locator(
        "input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
    )
    if not car_ok and visible_text_inputs.count() >= 1:
        _set_input_value(visible_text_inputs.nth(0), car_number)
    if not phone_ok and visible_text_inputs.count() >= 2:
        _set_input_value(visible_text_inputs.nth(1), phone)


def _fill_input_near_label(page, label_patterns: list[str], value: str) -> bool:
    for label in label_patterns:
        selectors = [
            f"xpath=//*[contains(normalize-space(.), '{label}')]/following::input[1]",
            f"xpath=//label[contains(normalize-space(.), '{label}')]/following::input[1]",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                _set_input_value(loc, value)
                current = loc.input_value().strip()
                if current:
                    return True
            except Exception:
                continue
    return False


def _set_input_value(locator, value: str) -> None:
    locator.fill(value)
    try:
        locator.dispatch_event("input")
        locator.dispatch_event("change")
    except Exception:
        pass


def _click_lookup_confirm(page) -> None:
    try:
        page.get_by_role("button", name="확인").first.click(timeout=1500)
    except Exception:
        page.locator("button:has-text('확인')").first.click(timeout=1500)




def _extract_booking_list_row(page, car_number: str) -> tuple[str, bool]:
    try:
        _wait_booking_list_rows(page, timeout_ms=3000)
        payload = page.evaluate(
            """(carNum) => {
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const rows = Array.from(document.querySelectorAll(".component-table-row.body.el-row--flex"));
                if (!rows.length) return { status: "row_not_found", cancel: false };

                const parsed = rows.map((row) => {
                  const cols = row.querySelectorAll(".col.el-col.el-col-24");
                  const car = cols[1] ? norm(cols[1].textContent) : "";
                  const status = norm((row.querySelector(".col.book") || {}).textContent || "");
                  const txt = norm(row.textContent);
                  const hasCancel =
                    !!row.querySelector("button.el-button--danger") ||
                    /예약\\s*취소/.test(txt) ||
                    txt.includes("예약취소");
                  return { car, status, hasCancel, txt };
                });

                const prefix = norm(carNum).slice(0, 5);
                let row = parsed[0];
                if (prefix) {
                  const matched = parsed.find((r) => r.car.includes(prefix) || r.txt.includes(prefix));
                  if (matched) row = matched;
                }

                let status = row.status || "unknown";
                if (!status) {
                  if (row.txt.includes("입차")) status = "입차";
                  else if (row.txt.includes("취소")) status = "취소";
                  else if (row.txt.includes("예약")) status = "예약";
                }

                const cancel = row.hasCancel || status === "예약";
                return { status: status || "unknown", cancel };
            }""",
            car_number,
        )
        return str(payload.get("status", "parse_failed")), bool(payload.get("cancel", False))
    except Exception:
        return "parse_failed", False


def _extract_booking_statuses(page, car_number: str) -> list[str]:
    try:
        payload = page.evaluate(
            """(carNum) => {
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const rows = Array.from(document.querySelectorAll(".component-table-row.body.el-row--flex"));
                const prefix = norm(carNum).slice(0, 5);
                const statuses = [];
                for (const row of rows) {
                  const cols = row.querySelectorAll(".col.el-col.el-col-24");
                  const car = cols[1] ? norm(cols[1].textContent) : "";
                  if (prefix && !(car.includes(prefix) || norm(row.textContent).includes(prefix))) continue;
                  const s = norm((row.querySelector(".col.book") || {}).textContent || "");
                  if (s) statuses.push(s);
                }
                return statuses;
            }""",
            car_number,
        )
        return [str(x) for x in (payload or [])]
    except Exception:
        return []


def _extract_booking_rows_snapshot(page, car_number: str) -> list[dict[str, str]]:
    try:
        payload = page.evaluate(
            """(carNum) => {
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const rows = Array.from(document.querySelectorAll(".component-table-row.body.el-row--flex"));
                const prefix = norm(carNum).slice(0, 5);
                const out = [];
                for (const row of rows) {
                    const cols = row.querySelectorAll(".col.el-col.el-col-24");
                    const no = cols[0] ? norm(cols[0].textContent) : "";
                    const car = cols[1] ? norm(cols[1].textContent) : "";
                    const applyDate = cols[2] ? norm(cols[2].textContent) : "";
                    if (prefix && !(car.includes(prefix) || norm(row.textContent).includes(prefix))) continue;
                    const status = norm((row.querySelector(".col.book") || {}).textContent || "");
                    out.push({ no, car, applyDate, status });
                }
                return out;
            }""",
            car_number,
        )
        return [dict(x) for x in (payload or [])]
    except Exception:
        return []


def _find_booking_row(page, car_number: str):
    # Preferred structure from booking-list page.
    rows = page.locator(".component-table-row.body.el-row--flex")
    if rows.count() == 0:
        return page.locator("tr:has-text('확인하기')").first

    prefix = (car_number or "").strip()[:5]
    if prefix:
        for i in range(rows.count()):
            r = rows.nth(i)
            try:
                car_cell = r.locator(".col.el-col.el-col-24").nth(1).inner_text().strip()
                if prefix in car_cell:
                    return r
            except Exception:
                continue

    # If there is a cancel button row, prefer it.
    for i in range(rows.count()):
        r = rows.nth(i)
        if r.locator("button.el-button--danger, button:has-text('예약취소'), button:has-text('예약 취소')").count() > 0:
            return r
    return rows.first


def run_booking_list_check(config: dict[str, Any], screenshot_dir: Path, booking_override: dict[str, Any] | None = None) -> dict[str, Any]:
    booking = dict(config.get("booking", {}))
    if booking_override:
        booking.update(booking_override)
    runtime = config["runtime"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot_path = screenshot_dir / f"verify_{ts}.png"
    result = {
        "ok": False,
        "message": "unknown",
        "status_text": "",
        "cancel_available": False,
        "screenshot_path": str(shot_path),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=runtime["headless"], slow_mo=runtime["slow_mo_ms"])
        context = browser.new_context(locale="ko-KR")
        page = context.new_page()
        page.set_default_timeout(runtime["timeout_ms"])
        try:
            page.goto(BOOKING_LIST_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            _fill_booking_lookup(page, str(booking.get("car_number", "")), str(booking.get("phone", "")))
            status_text, cancel_available = _extract_booking_list_row(page, str(booking.get("car_number", "")))
            result["status_text"] = status_text
            result["cancel_available"] = cancel_available
            result["ok"] = status_text in ("예약", "입차") or (cancel_available and status_text != "row_not_found")
            result["message"] = f"booking_list_status:{status_text};cancel_available={cancel_available}"
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        except Exception as exc:
            result["message"] = f"verify_exception:{exc}"
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        finally:
            context.close()
            browser.close()


def run_booking_list_cancel(config: dict[str, Any], screenshot_dir: Path, booking_override: dict[str, Any] | None = None) -> dict[str, Any]:
    booking = dict(config.get("booking", {}))
    if booking_override:
        booking.update(booking_override)
    runtime = config["runtime"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot_path = screenshot_dir / f"cancel_{ts}.png"
    result = {
        "ok": False,
        "message": "unknown",
        "screenshot_path": str(shot_path),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=runtime["headless"], slow_mo=runtime["slow_mo_ms"])
        context = browser.new_context(locale="ko-KR")
        page = context.new_page()
        page.set_default_timeout(runtime["timeout_ms"])
        page.on("dialog", lambda d: d.accept())
        try:
            page.goto(BOOKING_LIST_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            _fill_booking_lookup(page, str(booking.get("car_number", "")), str(booking.get("phone", "")))

            if not _click_cancel_action(page, str(booking.get("car_number", ""))):
                result["message"] = "cancel_action_not_found"
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            page.wait_for_timeout(800)

            # Secondary identity prompt (must target modal-layer inputs/buttons only).
            second_ok = _handle_cancel_identity_modal(
                page,
                car_number=str(booking.get("car_number", "")),
                phone=str(booking.get("phone", "")),
            )
            if not second_ok:
                result["message"] = "cancel_identity_modal_not_handled"
                page.screenshot(path=str(shot_path), full_page=True)
                return result
            page.wait_for_timeout(1200)
            _handle_final_confirm_modal(page)
            page.wait_for_timeout(1000)

            before_rows = _extract_booking_rows_snapshot(page, str(booking.get("car_number", "")))
            before_reserved = len([r for r in before_rows if r.get("status") == "예약"])

            # Re-check status
            _fill_booking_lookup(page, str(booking.get("car_number", "")), str(booking.get("phone", "")))
            status_text, _ = _extract_booking_list_row(page, str(booking.get("car_number", "")))
            after_rows = _extract_booking_rows_snapshot(page, str(booking.get("car_number", "")))
            after_reserved = len([r for r in after_rows if r.get("status") == "예약"])
            after_statuses = [r.get("status", "") for r in after_rows]

            # Strict success criteria:
            # 1) reserved row count decreased, OR
            # 2) no rows remain in "예약" while rows exist, OR
            # 3) parser directly says latest row is "취소".
            canceled = False
            if before_reserved > after_reserved:
                canceled = True
            elif after_rows and after_reserved == 0 and "취소" in after_statuses:
                canceled = True
            elif status_text == "취소":
                canceled = True

            result["ok"] = canceled
            result["message"] = (
                f"cancel_status:{status_text};"
                f"before_reserved={before_reserved};after_reserved={after_reserved};"
                f"after_statuses={','.join(after_statuses) if after_statuses else '-'}"
            )
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        except Exception as exc:
            result["message"] = f"cancel_exception:{exc}"
            page.screenshot(path=str(shot_path), full_page=True)
            return result
        finally:
            context.close()
            browser.close()


def _click_cancel_action(page, car_number: str) -> bool:
    row = _find_booking_row(page, car_number)
    if row.count() > 0:
        candidates = [
            row.locator("button.el-button--danger"),
            row.locator("button:has-text('예약취소')"),
            row.locator("button:has-text('예약 취소')"),
            row.locator("a:has-text('예약취소')"),
            row.locator("a:has-text('예약 취소')"),
        ]
        for loc in candidates:
            try:
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=1500, force=True)
                return True
            except Exception:
                continue

    selectors = [
        "button.el-button--danger:visible",
        "button:has-text('예약취소'):visible",
        "button:has-text('예약 취소'):visible",
        "a:has-text('예약취소'):visible",
        "a:has-text('예약 취소'):visible",
    ]
    for s in selectors:
        try:
            loc = page.locator(s).first
            if loc.count() == 0:
                continue
            loc.click(timeout=1500, force=True)
            return True
        except Exception:
            continue
    return False


def _handle_cancel_identity_modal(page, car_number: str, phone: str) -> bool:
    phone = re.sub(r"\D", "", phone or "")
    modal_selectors = [
        ".el-dialog:visible",
        ".modal:visible",
        ".layer-popup:visible",
        ".el-message-box:visible",
    ]
    modal = None
    for sel in modal_selectors:
        try:
            loc = page.locator(sel).last
            if loc.count() > 0:
                modal = loc
                break
        except Exception:
            continue
    if modal is None:
        # Fallback to page-level visible popup-ish container.
        try:
            modal = page.locator("div:visible:has-text('차량번호'):has-text('휴대폰')").last
        except Exception:
            return False

    try:
        _fill_input_in_scope(modal, ["차량번호"], car_number)
        _fill_input_in_scope(modal, ["휴대전화", "전화번호", "휴대폰"], phone)
    except Exception:
        return False

    # Click confirm inside modal only.
    btn_selectors = [
        "button:has-text('확인')",
        ".el-button--primary:has-text('확인')",
        ".el-message-box__btns .el-button--primary",
    ]
    for bs in btn_selectors:
        try:
            b = modal.locator(bs).first
            if b.count() == 0:
                continue
            b.click(timeout=1500, force=True)
            return True
        except Exception:
            continue
    return False


def _fill_input_in_scope(scope, label_patterns: list[str], value: str) -> bool:
    for label in label_patterns:
        selectors = [
            f"xpath=.//*[contains(normalize-space(.), '{label}')]/following::input[1]",
            f"xpath=.//label[contains(normalize-space(.), '{label}')]/following::input[1]",
        ]
        for sel in selectors:
            try:
                loc = scope.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.fill(value)
                try:
                    loc.dispatch_event("input")
                    loc.dispatch_event("change")
                except Exception:
                    pass
                return True
            except Exception:
                continue

    # Fallback: visible text inputs in modal
    try:
        inputs = scope.locator("input:visible:not([type='hidden'])")
        if inputs.count() > 0:
            for i in range(inputs.count()):
                loc = inputs.nth(i)
                ph = ""
                try:
                    ph = (loc.get_attribute("placeholder") or "").strip()
                except Exception:
                    pass
                if any(k in ph for k in label_patterns) or not ph:
                    loc.fill(value)
                    try:
                        loc.dispatch_event("input")
                        loc.dispatch_event("change")
                    except Exception:
                        pass
                    return True
    except Exception:
        pass
    return False


def _handle_final_confirm_modal(page) -> bool:
    # Final "정말 취소하시겠습니까?" style confirmation can be native dialog or custom modal.
    selectors = [
        ".el-message-box:visible .el-button--primary",
        ".el-message-box:visible button:has-text('확인')",
        ".modal:visible button:has-text('확인')",
        ".el-dialog:visible button:has-text('확인')",
        "button:has-text('확인'):visible",
    ]
    for s in selectors:
        try:
            btn = page.locator(s).last
            if btn.count() == 0:
                continue
            btn.click(timeout=1500, force=True)
            return True
        except Exception:
            continue
    return False


def _wait_booking_list_rows(page, timeout_ms: int = 5000) -> None:
    # booking-list result table can render asynchronously after "확인".
    try:
        page.wait_for_selector(
            "table tbody tr, table tr:has-text('확인하기'), .el-table__body tr, .el-table__row",
            timeout=timeout_ms,
        )
    except Exception:
        page.wait_for_timeout(400)
