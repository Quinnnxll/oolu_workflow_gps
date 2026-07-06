import { describe, expect, it } from "vitest";
import { parseCsv, serializeCsv } from "./csv";

describe("csv", () => {
  it("parses plain rows", () => {
    expect(parseCsv("a,b\n1,2")).toEqual([
      ["a", "b"],
      ["1", "2"],
    ]);
  });

  it("handles quoted commas, newlines, and escaped quotes", () => {
    const text = 'name,note\nalice,"hi, there"\nbob,"she said ""ok""\nthen left"';
    expect(parseCsv(text)).toEqual([
      ["name", "note"],
      ["alice", "hi, there"],
      ["bob", 'she said "ok"\nthen left'],
    ]);
  });

  it("round-trips through serialize", () => {
    const rows = [
      ["name", "note"],
      ["alice", "hi, there"],
      ["bob", 'quote " inside'],
    ];
    expect(parseCsv(serializeCsv(rows))).toEqual(rows);
  });
});
