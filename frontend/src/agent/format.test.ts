import { describe, expect, it } from "vitest";

import { action, count, dateTime, ms, percent, usd } from "./format";

describe("format", () => {
  it("writes times in UTC", () => {
    expect(dateTime("2026-10-03T09:15:00-05:00")).toBe("2026-10-03 14:15:00 UTC");
    expect(dateTime(null)).toBe("—");
    expect(dateTime("not a date")).toBe("not a date");
  });

  it("writes durations, shares, money and counts", () => {
    expect(ms(12.4)).toBe("12 ms");
    expect(ms(1500)).toBe("1.50 s");
    expect(ms(undefined)).toBe("—");
    expect(percent(0.456)).toBe("46%");
    expect(percent(null)).toBe("—");
    expect(usd("0.00041")).toBe("USD 0.0004");
    expect(usd(2, 2)).toBe("USD 2.00");
    expect(usd("")).toBe("—");
    expect(usd("n/a")).toBe("n/a");
    expect(count(1234567)).toBe("1,234,567");
    expect(count(null)).toBe("—");
  });

  it("names the actions it knows", () => {
    expect(action("ACT-02")).toBe("ACT-02 · Create dispute case");
    expect(action("ACT-99")).toBe("ACT-99");
  });
});
