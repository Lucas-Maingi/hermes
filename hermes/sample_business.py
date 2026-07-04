"""A realistic sample tenant used by tests, the demo, and the seeded dashboard.

Modeled on a Nairobi neighbourhood general store (a "duka") -- the exact kind
of SME this product is for.
"""

from hermes.business import Business, Product


def sample_business() -> Business:
    return Business(
        id="duka_demo",
        name="Mama Njeri Duka",
        currency="KES",
        products=[
            Product(name="Maize flour", price=180, unit="2kg", aliases=["unga", "ugali flour", "jogoo"]),
            Product(name="Sugar", price=160, unit="1kg", aliases=["sukari"]),
            Product(name="Cooking oil", price=350, unit="1L", aliases=["oil", "mafuta", "salad"]),
            Product(name="Rice", price=210, unit="2kg", aliases=["mchele", "pishori"]),
            Product(name="Bread", price=70, unit="400g", aliases=["mkate"]),
            Product(name="Milk", price=60, unit="500ml", aliases=["maziwa"]),
            Product(name="Tea leaves", price=120, unit="250g", aliases=["majani", "chai"]),
            Product(name="Bar soap", price=90, unit="800g", aliases=["sabuni", "washing soap"]),
            Product(name="Eggs", price=420, unit="tray of 30", aliases=["mayai"]),
            Product(name="Salt", price=40, unit="1kg", aliases=["chumvi"], in_stock=False),
        ],
        info={
            "hours": "We're open Monday to Saturday, 7am to 9pm, and Sundays 8am to 6pm.",
            "location": "We're on Thika Road, Kasarani, next to the Naivas supermarket.",
            "delivery": "We deliver free within Kasarani for orders over KES 500; otherwise KES 100.",
            "payment": "We accept M-Pesa (I can send you a prompt) and cash on delivery.",
            "returns": "Unopened items can be returned within 3 days with your M-Pesa receipt.",
            "contact": "You can reach the shop on 0712 000 000.",
        },
        mpesa_shortcode="",  # blank -> the M-Pesa simulator is used
        staff_handoff_number="254712000000",
    )
