"""Fictional products, with delivery values controlled by SwiftCart's server."""
from dataclasses import asdict, dataclass

from app.policy import HIGH_VALUE_THRESHOLD_INR


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    category: str
    description: str
    price_inr: int
    art: str


PRODUCTS = (
    Product("headphones", "Studio Wireless Headphones", "AUDIO", "Over-ear comfort. A quieter commute. Your everyday soundtrack.", 2999, "headphones"),
    Product("backpack", "Everyday Canvas Backpack", "CARRY", "A workday companion with room for the things that matter.", 1499, "backpack"),
    Product("watch", "Pulse Smartwatch", "WEARABLES", "Daily movement, favourite playlists, and a little more time outdoors.", 4999, "watch"),
    Product("laptop", "Airbook 14 Laptop", "COMPUTING", "A lightweight notebook for work, ideas, and everything in between.", 34999, "laptop"),
)


def catalog_items():
    return [{**asdict(product), "direct_handoff_required": product.price_inr >= HIGH_VALUE_THRESHOLD_INR}
            for product in PRODUCTS]


def find_product(product_id: str):
    return next((product for product in PRODUCTS if product.id == product_id), None)
