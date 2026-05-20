from typing import List, Literal, Optional
import re

from pydantic import BaseModel, FieldValidationInfo, field_validator, model_validator


def normalize_phone(phone: str) -> str:
    """Strip all non-digits, then remove leading country code 1 if 11 digits."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"Invalid phone number: must be 10 digits after normalization, got {len(digits)}")
    return digits


class LookupByPhoneRequest(BaseModel):
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)


class SaveOrUpdateCustomerRequest(BaseModel):
    phone_number: str
    first_name: str
    last_name: str
    address: Optional[str] = None   # maps to default_address in the DB
    notes: Optional[str] = None

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("first_name", "last_name")
    @classmethod
    def not_empty(cls, v: str, info: FieldValidationInfo) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} cannot be empty")
        return v.strip()


# ---------------------------------------------------------------------------
# /check-delivery-eligibility
# ---------------------------------------------------------------------------

class DeliveryEligibilityRequest(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("address cannot be blank")
        return v.strip()


# ---------------------------------------------------------------------------
# /create-order-cart
# ---------------------------------------------------------------------------

class CreateCartRequest(BaseModel):
    phone_number:          str
    order_type:            str                  # "pickup" or "delivery"
    customer_name:         str
    delivery_address:      Optional[str] = None  # normalized address (from check_delivery_eligibility)
    raw_delivery_address:  Optional[str] = None  # exactly what caller said — printed on receipt
    address_confidence:    Optional[str] = "high"  # 'high' | 'low'

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("order_type")
    @classmethod
    def validate_order_type(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("pickup", "delivery"):
            raise ValueError("order_type must be 'pickup' or 'delivery'")
        return v

    @field_validator("customer_name")
    @classmethod
    def validate_customer_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("customer_name cannot be blank")
        return v.strip()

    @model_validator(mode="after")
    def delivery_needs_address(self) -> "CreateCartRequest":
        if self.order_type == "delivery" and not self.delivery_address:
            raise ValueError("delivery_address is required for delivery orders")
        return self


# ---------------------------------------------------------------------------
# /add-item-to-cart
# ---------------------------------------------------------------------------

class AddItemRequest(BaseModel):
    cart_id:    int
    item_name:  str
    quantity:   int            = 1
    unit_price: float
    size:       Optional[str]        = None
    modifiers:  Optional[List[str]]  = None
    notes:      Optional[str]        = None

    @field_validator("item_name")
    @classmethod
    def validate_item_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("item_name cannot be blank")
        return v.strip()

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: int) -> int:
        if v < 1:
            raise ValueError("quantity must be at least 1")
        return v

    @field_validator("unit_price")
    @classmethod
    def validate_unit_price(cls, v: float) -> float:
        if v < 0:
            raise ValueError("unit_price cannot be negative")
        return round(v, 2)


# ---------------------------------------------------------------------------
# /get-cart-summary
# ---------------------------------------------------------------------------

class GetCartSummaryRequest(BaseModel):
    cart_id: int


# ---------------------------------------------------------------------------
# /update-cart-item
# ---------------------------------------------------------------------------

class UpdateCartItemRequest(BaseModel):
    cart_id:      int
    cart_item_id: int
    quantity:     Optional[int]       = None  # omit to keep current value
    size:         Optional[str]       = None  # omit to keep current value
    modifiers:    Optional[List[str]] = None  # omit to keep current value
    notes:        Optional[str]       = None  # omit to keep current value

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 1:
            raise ValueError("quantity must be at least 1")
        return v


# ---------------------------------------------------------------------------
# /remove-cart-item
# ---------------------------------------------------------------------------

class RemoveCartItemRequest(BaseModel):
    cart_id:      int
    cart_item_id: int


# ---------------------------------------------------------------------------
# /clear-cart
# ---------------------------------------------------------------------------

class ClearCartRequest(BaseModel):
    cart_id: int


# ---------------------------------------------------------------------------
# /cancel-cart
# ---------------------------------------------------------------------------

class CancelCartRequest(BaseModel):
    cart_id: int


# ---------------------------------------------------------------------------
# /confirm-order
# ---------------------------------------------------------------------------

class ConfirmOrderRequest(BaseModel):
    cart_id: int


# ---------------------------------------------------------------------------
# /apply-coupon
# ---------------------------------------------------------------------------

class ApplyCouponRequest(BaseModel):
    cart_id:      int
    coupon_type:  Literal["percent", "flat"]
    coupon_value: float   # percentage (e.g. 10 = 10%) or dollar amount (e.g. 5 = $5)
    description:  str     # what the caller said, e.g. "10% off any order"

    @field_validator("coupon_value")
    @classmethod
    def positive_value(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("coupon_value must be greater than 0")
        return round(v, 2)

    @field_validator("description")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description cannot be blank")
        return v.strip()


class RemoveCouponRequest(BaseModel):
    cart_id: int


# ---------------------------------------------------------------------------
# /set-order-time
# ---------------------------------------------------------------------------

class SetOrderTimeRequest(BaseModel):
    cart_id:     int
    spoken_time: str   # e.g. "next Friday at 6 PM", "tomorrow at noon", "tonight at 7:30"

    @field_validator("spoken_time")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("spoken_time cannot be blank")
        return v.strip()
