import { describe, expect, it } from "vitest";
import { conciseName, keywords } from "./naming";

describe("naming", () => {
  it("distills a task sentence into ordered keywords", () => {
    expect(
      keywords("convert the quarterly report to pdf and email it to john"),
    ).toEqual(["convert", "quarterly", "report", "pdf"]);
  });

  it("names by keywords, never the whole sentence", () => {
    expect(
      conciseName("convert the quarterly report to pdf and email it please"),
    ).toBe("Convert Quarterly Report Pdf");
    expect(conciseName("fetch http latest exchange rates")).toBe(
      "Fetch Http Latest Exchange",
    );
  });

  it("keeps order and drops duplicates", () => {
    expect(conciseName("pdf report report pdf convert")).toBe(
      "Pdf Report Convert",
    );
  });

  it("falls back to trimmed text when only stopwords remain", () => {
    expect(conciseName("do it for me please")).toBe("do it for me please");
    expect(conciseName("")).toBe("");
  });
});
