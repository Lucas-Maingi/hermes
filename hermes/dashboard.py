"""Streamlit app: a WhatsApp-style chat simulator that drives the real agent,
plus the shop-owner dashboard.

Run: streamlit run hermes/dashboard.py
The simulator talks to the exact same Agent the WhatsApp webhook uses, and an
"enter PIN" button drives the M-Pesa simulator through a real payment, so the
whole order -> pay -> confirm loop is demonstrable with no accounts.
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from hermes.models import Conversation, PaymentStatus, Role
from hermes.runtime import Runtime

st.set_page_config(page_title="Hermes - WhatsApp AI for SMEs", page_icon="🛒", layout="wide")


@st.cache_resource
def get_runtime() -> Runtime:
    return Runtime(db_path=os.getenv("HERMES_DB", "hermes.db"))


rt = get_runtime()
business = rt.default_business

st.title("Hermes")
st.caption(f"WhatsApp AI commerce & support agent - demo shop: **{business.name}**")

providers = f"LLM: `{type(rt.llm).__name__}` · M-Pesa: `{type(rt.mpesa).__name__}`"
st.caption(providers + "  (real providers activate automatically when credentials are set)")

tab_chat, tab_dash = st.tabs(["💬 Try the agent", "📊 Owner dashboard"])

# -- Chat simulator -----------------------------------------------------------

with tab_chat:
    left, right = st.columns([2, 1])

    with left:
        if "conv" not in st.session_state:
            st.session_state.conv = Conversation(customer_phone="254712345678", channel="simulator")

        conv: Conversation = st.session_state.conv

        st.markdown("**WhatsApp chat** (you are the customer)")
        for msg in conv.messages:
            avatar = "🧑" if msg.role == Role.CUSTOMER else "🛒"
            with st.chat_message("user" if msg.role == Role.CUSTOMER else "assistant", avatar=avatar):
                st.write(msg.text)

        prompt = st.chat_input("Type a message... e.g. 'nataka 2 unga' or 'how much is sugar?'")
        if prompt:
            agent = rt.agent_for(business)
            agent.handle_message(conv, prompt)
            rt.store.save(conv)
            st.rerun()

        # M-Pesa PIN simulation when a payment is pending.
        if conv.payment and conv.payment.status == PaymentStatus.PENDING:
            st.info(f"An M-Pesa prompt for KES {conv.payment.amount:,.0f} is on the customer's phone.")
            cols = st.columns(2)
            if cols[0].button("📲 Enter PIN & pay", type="primary"):
                rt.mpesa.complete_payment(conv.payment.checkout_request_id, success=True)
                rt.agent_for(business).handle_message(conv, "have you received the payment?")
                rt.store.save(conv)
                st.rerun()
            if cols[1].button("✖ Cancel on phone"):
                rt.mpesa.complete_payment(conv.payment.checkout_request_id, success=False)
                rt.agent_for(business).handle_message(conv, "did the payment go through?")
                rt.store.save(conv)
                st.rerun()

        if st.button("Start a new conversation"):
            st.session_state.conv = Conversation(customer_phone="254712345678", channel="simulator")
            st.rerun()

    with right:
        st.markdown("**Order**")
        if conv.order and conv.order.items:
            df = pd.DataFrame(
                [{"item": i.name, "qty": i.quantity, "line": f"{business.currency} {i.line_total:,.0f}"} for i in conv.order.items]
            )
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.metric("Order total", f"{business.currency} {conv.order.total:,.0f}")
            st.caption(f"Status: {conv.order.status.value}")
        else:
            st.caption("No items yet.")

        if conv.payment:
            st.markdown("**Payment**")
            st.write(f"Status: `{conv.payment.status.value}`")
            if conv.payment.receipt:
                st.success(f"M-Pesa receipt: {conv.payment.receipt}")

        if conv.needs_human:
            st.warning("Escalated to a human staff member.")

        st.markdown("**Catalog** (try these)")
        st.dataframe(
            pd.DataFrame(
                [{"product": p.name, "price": f"{business.currency} {p.price:,.0f}", "in stock": p.in_stock} for p in business.products]
            ),
            hide_index=True,
            use_container_width=True,
        )

# -- Owner dashboard ----------------------------------------------------------

with tab_dash:
    m = rt.store.metrics()
    if m["total_conversations"] == 0:
        st.info("No conversations yet. Chat with the agent (or seed demo data) to populate this.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Conversations", m["total_conversations"])
        c2.metric("Deflection rate", f"{m['deflection_rate']:.0%}", help="Handled without a human")
        c3.metric("Orders paid", m["orders_paid"])
        c4.metric("Revenue collected", f"{business.currency} {m['revenue_collected']:,.0f}")

        st.caption(
            f"{m['deflected']} of {m['total_conversations']} conversations handled end-to-end by the "
            f"agent; {m['escalations']} handed to a human."
        )

        st.markdown("**Recent conversations**")
        rows = []
        for c in rt.store.list_conversations(limit=100):
            rows.append(
                {
                    "phone": c.customer_phone,
                    "channel": c.channel,
                    "messages": len(c.messages),
                    "order": f"{business.currency} {c.order.total:,.0f}" if c.order and c.order.items else "-",
                    "status": c.order.status.value if c.order and c.order.items else "-",
                    "paid": bool(c.payment and c.payment.status == PaymentStatus.SUCCESS),
                    "escalated": c.needs_human,
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
