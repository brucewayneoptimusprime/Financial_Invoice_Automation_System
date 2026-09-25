"""A scripted stand-in for the model that answers each pipeline role from what it is GIVEN (never from memory): extraction from a
recorded reply, and the explainer and drafter from the digest in the request. Tests can corrupt either role's reply to prove the
checks hold. No network, ever."""
import json
from typing import Callable

from tests.llm.fakes import ok_response


def good_explanation(payload: dict) -> dict:
    facts = payload["facts"]
    triggered = [f for f in facts if f["severity"] > 0 and f["kind"] != "decision"]
    cited = triggered or [f for f in facts if f["kind"] == "summary"][:1]
    decision = payload["decision"]
    summary = {"approve": "All checks passed, so the invoice is approved.", "review": "This invoice needs a person to review it.",
               "request_info": "More information is needed from the vendor.", "reject": "This invoice cannot be processed."}[decision]
    return {"summary": summary, "reasons": [{"text": f["text"], "facts": [f["id"]]} for f in cited], "next_step": payload["next_step_hint"]}


def good_draft(payload: dict) -> dict:
    inv = payload.get("invoice", {})
    name, number = inv.get("vendor_name"), inv.get("number")
    bullets = "\n".join(f"- {r['text']}" for r in payload["requests"])
    if payload["decision"] == "reject":
        body = f"Dear {name or 'Sir or Madam'},\n\nWe are unable to process invoice {number}:\n\n{bullets}\n\nPlease reply if you believe this is a mistake.\n\nKind regards,\nAccounts Payable"
        subject = f"Invoice {number}: unable to process"
    else:
        body = (f"Dear {name or 'Sir or Madam'},\n\nThank you for invoice {number}. We need the following:\n\n{bullets}\n\n"
                "Please reply with the information or a corrected invoice.\n\nKind regards,\nAccounts Payable")
        subject = f"Invoice {number}: information needed"
    return {"subject": subject, "body": body, "covers": [r["id"] for r in payload["requests"]]}


class ModelDouble:
    """`explain` / `draft` are callables (payload -> reply dict or raw text) or None for the good reply. A callable can also return a
    list of replies to serve one per call (to test the repair retry)."""

    def __init__(self, extraction: dict, explain: Callable | None = None, draft: Callable | None = None, tokens=(6800, 920), role_tokens=(1300, 380)):
        self.extraction, self.explain, self.draft = extraction, explain, draft
        self.tokens, self.role_tokens = tokens, role_tokens
        self.requests = []
        self.calls = {"extract": 0, "explain": 0, "draft": 0}

    def complete(self, request):
        self.requests.append(request)
        purpose = request.purpose
        self.calls[purpose] = self.calls.get(purpose, 0) + 1
        if purpose == "extract":
            return ok_response(json.dumps(self.extraction, ensure_ascii=False), input_tokens=self.tokens[0], output_tokens=self.tokens[1])
        payload = json.loads(request.parts[0].text)
        make = self.explain if purpose == "explain" else self.draft
        reply = (good_explanation if purpose == "explain" else good_draft)(payload) if make is None else make(payload, self.calls[purpose])
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return ok_response(text, input_tokens=self.role_tokens[0], output_tokens=self.role_tokens[1])

    def of(self, purpose):
        return [r for r in self.requests if r.purpose == purpose]
