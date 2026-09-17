"""公历 → 农历转换（1900–2098，纯 stdlib，无新依赖——设计方案 §7.3 依赖白名单纪律）。

农历编码表：每个 int 自低位起——
- bit 0-3：闰月月份（0 = 当年无闰月）
- bit 4-15：十二个月大小月标记（bit(16-m) 为 1 → 第 m 月大月 30 天）
- bit 16：闰月大小月（1 = 30 天；无闰月时无意义）
基准日：1900-01-31 = 农历 1900 年正月初一。

表经 lunardate 库全量比对校验（72654 天逐日一致）后固化，算法独立无第三方依赖。
支持范围：1900-01-31 ~ 2098-12-31（2098 腊月结束于 2099 年初）。
"""

from datetime import date, datetime

_LUNAR_INFO: tuple[int, ...] = (
    0x04BD8,
    0x04AE0,
    0x0A570,
    0x054D5,
    0x0D260,
    0x0D950,
    0x16554,
    0x056A0,
    0x09AD0,
    0x055D2,
    0x04AE0,
    0x0A5B6,
    0x0A4D0,
    0x0D250,
    0x1D255,
    0x0B540,
    0x0D6A0,
    0x0ADA2,
    0x095B0,
    0x14977,
    0x04970,
    0x0A4B0,
    0x0B4B5,
    0x06A50,
    0x06D40,
    0x1AB54,
    0x02B60,
    0x09570,
    0x052F2,
    0x04970,
    0x06566,
    0x0D4A0,
    0x0EA50,
    0x06E95,
    0x05AD0,
    0x02B60,
    0x186E3,
    0x092E0,
    0x1C8D7,
    0x0C950,
    0x0D4A0,
    0x1D8A6,
    0x0B550,
    0x056A0,
    0x1A5B4,
    0x025D0,
    0x092D0,
    0x0D2B2,
    0x0A950,
    0x0B557,
    0x06CA0,
    0x0B550,
    0x15355,
    0x04DA0,
    0x0A5D0,
    0x14573,
    0x052B0,
    0x0A9A8,
    0x0E950,
    0x06AA0,
    0x0AEA6,
    0x0AB50,
    0x04B60,
    0x0AAE4,
    0x0A570,
    0x05260,
    0x0F263,
    0x0D950,
    0x05B57,
    0x056A0,
    0x096D0,
    0x04DD5,
    0x04AD0,
    0x0A4D0,
    0x0D4D4,
    0x0D250,
    0x0D558,
    0x0B540,
    0x0B5A0,
    0x195A6,
    0x095B0,
    0x049B0,
    0x0A974,
    0x0A4B0,
    0x0B27A,
    0x06A50,
    0x06D40,
    0x0AF46,
    0x0AB60,
    0x09570,
    0x04AF5,
    0x04970,
    0x064B0,
    0x074A3,
    0x0EA50,
    0x06B58,
    0x05AC0,
    0x0AB60,
    0x096D5,
    0x092E0,
    0x0C960,
    0x0D954,
    0x0D4A0,
    0x0DA50,
    0x07552,
    0x056A0,
    0x0ABB7,
    0x025D0,
    0x092D0,
    0x0CAB5,
    0x0A950,
    0x0B4A0,
    0x0BAA4,
    0x0AD50,
    0x055D9,
    0x04BA0,
    0x0A5B0,
    0x15176,
    0x052B0,
    0x0A930,
    0x07954,
    0x06AA0,
    0x0AD50,
    0x05B52,
    0x04B60,
    0x0A6E6,
    0x0A4E0,
    0x0D260,
    0x0EA65,
    0x0D530,
    0x05AA0,
    0x076A3,
    0x096D0,
    0x04AFB,
    0x04AD0,
    0x0A4D0,
    0x1D0B6,
    0x0D250,
    0x0D520,
    0x0DD45,
    0x0B5A0,
    0x056D0,
    0x055B2,
    0x049B0,
    0x0A577,
    0x0A4B0,
    0x0AA50,
    0x1B255,
    0x06D20,
    0x0ADA0,
    0x14B63,
    0x09370,
    0x049F8,
    0x04970,
    0x064B0,
    0x168A6,
    0x0EA50,
    0x06AA0,
    0x1A6C4,
    0x0AAE0,
    0x092E0,
    0x0D2E3,
    0x0C960,
    0x0D557,
    0x0D4A0,
    0x0DA50,
    0x05D55,
    0x056A0,
    0x0A6D0,
    0x055D4,
    0x052D0,
    0x0A9B8,
    0x0A950,
    0x0B4A0,
    0x0B6A6,
    0x0AD50,
    0x055A0,
    0x0ABA4,
    0x0A5B0,
    0x052B0,
    0x0B273,
    0x06930,
    0x07337,
    0x06AA0,
    0x0AD50,
    0x14B55,
    0x04B60,
    0x0A570,
    0x054E4,
    0x0D160,
    0x0E968,
    0x0D520,
    0x0DAA0,
    0x16AA6,
    0x056D0,
    0x04AE0,
    0x0A9D4,
    0x0A2D0,
    0x0D150,
)

_BASE_DATE = date(1900, 1, 31)  # 农历 1900 年正月初一

# 天干地支（纪年）：年干 = (年 - 4) % 10，年支 = (年 - 4) % 12
_GAN = "甲乙丙丁戊己庚辛壬癸"
_ZHI = "子丑寅卯辰巳午未申酉戌亥"
_MONTH_NAMES = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
_DIGITS_CN = "〇一二三四五六七八九"
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _leap_month(year: int) -> int:
    return _LUNAR_INFO[year - 1900] & 0xF


def _leap_month_days(year: int) -> int:
    """闰月天数；无闰月返回 0（经典算法陷阱：无闰月年份不可计入闰月天数）。"""
    if _leap_month(year) == 0:
        return 0
    return 30 if _LUNAR_INFO[year - 1900] & 0x10000 else 29


def _month_days(year: int, month: int) -> int:
    """month 1-12 的天数（不含闰月）。"""
    return 30 if _LUNAR_INFO[year - 1900] & (0x10000 >> month) else 29


def _year_days(year: int) -> int:
    return sum(_month_days(year, m) for m in range(1, 13)) + _leap_month_days(year)


def _solar_to_lunar(d: date) -> tuple[int, int, int, bool]:
    """公历日期 → (农历年, 月, 日, 是否闰月)。支持 1900-01-31 ~ 2098-12-31。"""
    offset = (d - _BASE_DATE).days
    if offset < 0:
        raise ValueError(f"不支持 1900-01-31 之前的日期：{d}")
    year = 1900
    while True:
        if year > 2098:
            raise ValueError(f"不支持 2098 年之后的日期：{d}")
        year_days = _year_days(year)
        if offset < year_days:
            break
        offset -= year_days
        year += 1
    # offset = 距该农历年正月初一的天数（0 起）；按月序列（闰月排在同名月之后）定位
    leap = _leap_month(year)
    for m in range(1, 13):
        days = _month_days(year, m)
        if offset < days:
            return year, m, offset + 1, False
        offset -= days
        if m == leap:
            leap_days = _leap_month_days(year)
            if offset < leap_days:
                return year, m, offset + 1, True
            offset -= leap_days
    raise AssertionError(f"农历月序列耗尽（{_year_days(year)} 天）不应发生：{d}")


def _year_cn(year: int) -> str:
    return "".join(_DIGITS_CN[int(c)] for c in str(year))


def _month_name(month: int, is_leap: bool) -> str:
    prefix = "闰" if is_leap else ""
    return f"{prefix}{_MONTH_NAMES[month - 1]}月"


_DAY_ONES = "一二三四五六七八九十"


def _day_name(day: int) -> str:
    if day <= 10:
        return f"初{_DAY_ONES[day - 1]}"
    if day < 20:
        return f"十{_DAY_ONES[day - 11]}"
    if day == 20:
        return "二十"
    if day < 30:
        return f"廿{_DAY_ONES[day - 21]}"
    return "三十"


def _gan_zhi_year(lunar_year: int) -> str:
    return f"{_GAN[(lunar_year - 4) % 10]}{_ZHI[(lunar_year - 4) % 12]}"


def format_datetime(now: datetime) -> str:
    """格式化：农历（干支纪年+中文月日）+ 公历 + 星期几。"""
    ly, lm, ld, leap = _solar_to_lunar(now.date())
    return (
        f"【农历】{_gan_zhi_year(ly)}年（{_year_cn(ly)}）{_month_name(lm, leap)}{_day_name(ld)} "
        f"{now:%H:%M:%S}；"
        f"【公历】{now:%Y-%m-%d %H:%M:%S}；"
        f"星期{_WEEKDAYS[now.weekday()]}"
    )
