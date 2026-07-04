from hermes.llm.mock import MockLLM, default_tools


def user(text):
    return [{"role": "user", "content": text}]


def call_names(resp):
    return [tc.name for tc in resp.tool_calls]


class TestMockIntents:
    def setup_method(self):
        self.llm = MockLLM()
        self.tools = default_tools()

    def test_greeting_returns_text_no_tools(self):
        resp = self.llm.complete("sys", user("Hi there"), self.tools)
        assert resp.text
        assert not resp.wants_tools

    def test_question_calls_knowledge_lookup(self):
        resp = self.llm.complete("sys", user("How much is maize flour?"), self.tools)
        assert "lookup_knowledge" in call_names(resp)

    def test_swahili_question_calls_knowledge_lookup(self):
        resp = self.llm.complete("sys", user("bei ya unga ni?"), self.tools)
        assert "lookup_knowledge" in call_names(resp)

    def test_order_intent_captures_items(self):
        resp = self.llm.complete("sys", user("I want 2 maize flour and 3 sugar"), self.tools)
        names = call_names(resp)
        assert names.count("capture_order") == 2

    def test_order_captures_quantity_and_item_args(self):
        resp = self.llm.complete("sys", user("nataka 5 unga"), self.tools)
        capture = [tc for tc in resp.tool_calls if tc.name == "capture_order"][0]
        assert capture.arguments["quantity"] == 5
        assert "unga" in capture.arguments["item"].lower()

    def test_pay_intent_calls_stk_push(self):
        resp = self.llm.complete("sys", user("I want to pay now"), self.tools)
        assert "mpesa_stk_push" in call_names(resp)

    def test_swahili_pay_intent_calls_stk_push(self):
        resp = self.llm.complete("sys", user("nlipe na mpesa"), self.tools)
        assert "mpesa_stk_push" in call_names(resp)

    def test_human_request_escalates(self):
        resp = self.llm.complete("sys", user("Can I talk to a person?"), self.tools)
        assert "escalate_to_human" in call_names(resp)

    def test_tool_result_is_narrated_as_text(self):
        messages = [
            {"role": "user", "content": "how much is sugar"},
            {"role": "tool", "content": "Sugar 1kg is KES 160."},
        ]
        resp = self.llm.complete("sys", messages, self.tools)
        assert not resp.wants_tools
        assert "160" in resp.text

    def test_deterministic_same_input_same_output(self):
        a = self.llm.complete("sys", user("I want 2 bread"), self.tools)
        b = self.llm.complete("sys", user("I want 2 bread"), self.tools)
        assert call_names(a) == call_names(b)


class TestDefaultTools:
    def test_exposes_the_five_core_tools(self):
        names = {t.name for t in default_tools()}
        assert names == {
            "lookup_knowledge",
            "capture_order",
            "mpesa_stk_push",
            "check_payment",
            "escalate_to_human",
        }
