"""Rules settings with layered defaults (SETTINGS_PLAN; SPEC section 11 items 91-94): global default -> PO override, the most specific
wins. The engine reads the effective values through `engine.loader.load_effective`; nothing else in the engine knows about layers."""
