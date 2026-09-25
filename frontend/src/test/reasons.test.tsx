import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { humanizeFields, plainReason, reviewReasons } from "../reasons";
import { DecisionBanner, ResultView } from "../components/Result";
import { VIEWS } from "./fixtures";

describe("plainReason", () => {
  it("strips the rule prefix and keeps the ids for the details", () => {
    const r = plainReason("r_po_found (Invoice matches a purchase order) - matched_without_reference, severity 1: The invoice has no PO reference; "
      + "purchase order PO-SS-001 is a confident, unambiguous match on vendor, amount and lines. A person should confirm it.", ["F2"]);
    expect(r.text).toBe("The invoice has no PO reference; purchase order PO-SS-001 is a confident, unambiguous match on vendor, amount and lines. A person should confirm it.");
    expect(r.technical).toEqual({ ruleId: "r_po_found", ruleName: "Invoice matches a purchase order", outcome: "matched_without_reference",
                                  severity: 1, facts: ["F2"] });
  });
  it("handles the engine floor form and turns field ids into labels", () => {
    const r = plainReason("Engine floor (required_fields_low_confidence): required fields below the confidence threshold: vendor_name, invoice_date.", ["F2"]);
    expect(r.text).toBe("Required fields below the confidence threshold: vendor, invoice date.");
    expect(r.technical?.floorCode).toBe("required_fields_low_confidence");
    expect(r.text).not.toMatch(/_/);
  });
  it("keeps a model-written reason as it is, with its facts in the details", () => {
    const r = plainReason("The invoice does not print a purchase-order number, so a person should confirm the match", ["F4"]);
    expect(r.text).toBe("The invoice does not print a purchase-order number, so a person should confirm the match.");
    expect(r.technical).toEqual({ facts: ["F4"] });
  });
  it("never hides an unknown form", () => {
    expect(plainReason("Approval withheld: the PO balance changed.").text).toBe("Approval withheld: the PO balance changed.");
    expect(plainReason("Approval withheld: x").technical).toBeNull();
  });
  it("labels every header field id", () => {
    expect(humanizeFields("vendor_tax_id, po_reference and total")).toBe("vendor tax ID, PO reference and total");
  });
});

describe("reviewReasons", () => {
  it("splits the review-queue line into plain parts", () => {
    const raw = VIEWS.iq.actions.review[0].reason;
    const parts = reviewReasons(raw);
    expect(parts).toHaveLength(3);
    expect(parts.map((p) => p.technical?.ruleId)).toEqual(["engine_floor", "r_extraction_confidence", "r_po_found"]);
    expect(parts[1].text).toBe("Low extraction confidence on: vendor (0.30 < 0.80), invoice date (0.50 < 0.80).");
    for (const p of parts) expect(p.text).not.toMatch(/\b[a-z]+_[a-z_]+\b|severity/);
  });
});

describe("the Why bullets in the UI", () => {
  it("show plain sentences by default and the ids only on request", () => {
    render(<DecisionBanner view={VIEWS.iq} />);
    const why = document.querySelector(".banner-why")!;
    expect(why.textContent).not.toMatch(/r_po_found|required_fields_low_confidence|severity|matched_without_reference/);
    expect(screen.getAllByRole("button", { name: "Technical details" })).toHaveLength(3);
    fireEvent.click(screen.getAllByRole("button", { name: "Technical details" })[2]);
    expect(why.textContent).toMatch(/r_po_found/);
    expect(why.textContent).toMatch(/matched_without_reference/);
    expect(screen.getByText("Severity")).toBeInTheDocument();
  });
  it("the review-queue reason gets the same treatment", () => {
    render(<ResultView view={VIEWS.iq} />);
    const action = [...document.querySelectorAll(".action")].find((a) => a.textContent?.includes("Review queue"))!;
    expect(action.textContent).not.toMatch(/r_extraction_confidence|low_confidence\)/);
    expect(action.textContent).toMatch(/Low extraction confidence on: vendor/);
  });
});
