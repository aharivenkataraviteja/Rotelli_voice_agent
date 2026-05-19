#!/bin/bash

VAPI_KEY="${VAPI_KEY:-your-vapi-api-key-here}"
BASE_URL="${BASE_URL:-https://your-ngrok-url.ngrok-free.dev}"

echo "Registering all 11 Vapi tools..."
echo "Base URL: $BASE_URL"
echo ""

# ─────────────────────────────────────────────
# TOOL 1 — lookup_customer_by_phone
# ─────────────────────────────────────────────
echo "1/11 — lookup_customer_by_phone"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "lookup_customer_by_phone",
      "description": "Look up a customer by phone number to retrieve their saved name and delivery address.",
      "parameters": {
        "type": "object",
        "properties": {
          "phone_number": {
            "type": "string",
            "description": "10-digit phone number, digits only, e.g. 5615551234"
          }
        },
        "required": ["phone_number"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/lookup-customer-by-phone" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 2 — save_or_update_customer
# ─────────────────────────────────────────────
echo "2/11 — save_or_update_customer"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "save_or_update_customer",
      "description": "Save a new customer or update an existing one by phone number. Call this after collecting the caller'\''s name and optionally their address.",
      "parameters": {
        "type": "object",
        "properties": {
          "phone_number": {
            "type": "string",
            "description": "10-digit phone number, digits only"
          },
          "first_name": {
            "type": "string",
            "description": "Customer first name"
          },
          "last_name": {
            "type": "string",
            "description": "Customer last name"
          },
          "address": {
            "type": "string",
            "description": "Customer default delivery address (optional)"
          },
          "notes": {
            "type": "string",
            "description": "Any special notes about the customer (optional)"
          }
        },
        "required": ["phone_number", "first_name", "last_name"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/save-or-update-customer" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 3 — check_delivery_eligibility
# ─────────────────────────────────────────────
echo "3/11 — check_delivery_eligibility"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "check_delivery_eligibility",
      "description": "Check if a delivery address is within the restaurant delivery radius. Always call this before creating a delivery cart.",
      "parameters": {
        "type": "object",
        "properties": {
          "address": {
            "type": "string",
            "description": "Full delivery address provided by the caller, e.g. 123 Main St, Boca Raton FL"
          }
        },
        "required": ["address"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/check-delivery-eligibility" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 4 — create_order_cart
# ─────────────────────────────────────────────
echo "4/11 — create_order_cart"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "create_order_cart",
      "description": "Create a new order cart for the caller. Call this once per order after confirming pickup or delivery.",
      "parameters": {
        "type": "object",
        "properties": {
          "phone_number": {
            "type": "string",
            "description": "10-digit phone number, digits only"
          },
          "customer_name": {
            "type": "string",
            "description": "Full name of the customer, e.g. John Smith"
          },
          "order_type": {
            "type": "string",
            "enum": ["pickup", "delivery"],
            "description": "Whether this is a pickup or delivery order"
          },
          "delivery_address": {
            "type": "string",
            "description": "Delivery address — required only if order_type is delivery"
          }
        },
        "required": ["phone_number", "customer_name", "order_type"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/create-order-cart" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 5 — add_item_to_cart
# ─────────────────────────────────────────────
echo "5/11 — add_item_to_cart"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "add_item_to_cart",
      "description": "Add a menu item to the active cart. Call once per item.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID returned from create_order_cart"
          },
          "item_name": {
            "type": "string",
            "description": "Name of the menu item, e.g. Margherita Pizza"
          },
          "quantity": {
            "type": "integer",
            "description": "Number of this item to add",
            "minimum": 1
          },
          "unit_price": {
            "type": "number",
            "description": "Price per unit in dollars, e.g. 16.99"
          },
          "size": {
            "type": "string",
            "description": "Size of the item if applicable, e.g. small, medium, large"
          },
          "modifiers": {
            "type": "array",
            "items": { "type": "string" },
            "description": "List of modifiers, e.g. [\"no onions\", \"extra cheese\"]"
          },
          "notes": {
            "type": "string",
            "description": "Any special instructions for this item"
          }
        },
        "required": ["cart_id", "item_name", "quantity", "unit_price"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/add-item-to-cart" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 6 — get_cart_summary
# ─────────────────────────────────────────────
echo "6/11 — get_cart_summary"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "get_cart_summary",
      "description": "Get the full cart summary including all items, subtotal, delivery fee, and final total. Always call this before reading the order back to the caller.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID to retrieve the summary for"
          }
        },
        "required": ["cart_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/get-cart-summary" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 7 — update_cart_item
# ─────────────────────────────────────────────
echo "7/11 — update_cart_item"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "update_cart_item",
      "description": "Update an existing cart item — change quantity, size, modifiers, or notes. Only include fields you want to change.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID"
          },
          "cart_item_id": {
            "type": "integer",
            "description": "Cart item ID from get_cart_summary"
          },
          "quantity": {
            "type": "integer",
            "description": "New quantity",
            "minimum": 1
          },
          "size": {
            "type": "string",
            "description": "New size"
          },
          "modifiers": {
            "type": "array",
            "items": { "type": "string" },
            "description": "New list of modifiers (replaces existing)"
          },
          "notes": {
            "type": "string",
            "description": "New special instructions"
          }
        },
        "required": ["cart_id", "cart_item_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/update-cart-item" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 8 — remove_cart_item
# ─────────────────────────────────────────────
echo "8/11 — remove_cart_item"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "remove_cart_item",
      "description": "Remove an item entirely from the cart. Use cart_item_id from get_cart_summary.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID"
          },
          "cart_item_id": {
            "type": "integer",
            "description": "Cart item ID to remove"
          }
        },
        "required": ["cart_id", "cart_item_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/remove-cart-item" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 9 — clear_cart
# ─────────────────────────────────────────────
echo "9/11 — clear_cart"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "clear_cart",
      "description": "Remove all items from the cart without cancelling it. Use when the caller wants to start the order over.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID to clear"
          }
        },
        "required": ["cart_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/clear-cart" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 10 — cancel_cart
# ─────────────────────────────────────────────
echo "10/11 — cancel_cart"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "cancel_cart",
      "description": "Cancel the cart entirely. Use when the caller abandons the order or the call ends without confirmation.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID to cancel"
          }
        },
        "required": ["cart_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/cancel-cart" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 11 — confirm_order
# ─────────────────────────────────────────────
echo "11/11 — confirm_order"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "confirm_order",
      "description": "Confirm the order, lock the cart, and send it to the POS system. Only call this after the caller explicitly says yes to the order summary.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID to confirm"
          }
        },
        "required": ["cart_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/confirm-order" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 12 — set_order_time
# ─────────────────────────────────────────────
echo "12/12 — set_order_time"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "set_order_time",
      "description": "Set a future pickup or delivery time for a scheduled order. Pass the spoken time phrase exactly as the caller said it — the backend parses it. Call this after create_order_cart and before confirm_order when the caller wants to schedule for a future time.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID returned from create_order_cart"
          },
          "spoken_time": {
            "type": "string",
            "description": "The time phrase as spoken by the caller, e.g. next Friday at 6 PM, tomorrow at noon, tonight at 7:30, Saturday afternoon"
          }
        },
        "required": ["cart_id", "spoken_time"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/set-order-time" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"


# ─────────────────────────────────────────────
# TOOL 13 — apply_coupon
# ─────────────────────────────────────────────
echo "13/14 — apply_coupon"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "apply_coupon",
      "description": "Apply a customer coupon to the active cart. Call this when the caller says they have a coupon. Returns the updated cart summary with the discounted total.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID to apply the coupon to"
          },
          "coupon_type": {
            "type": "string",
            "enum": ["percent", "flat"],
            "description": "percent = percentage off (e.g. 10 means 10% off), flat = fixed dollar amount off (e.g. 5 means $5 off)"
          },
          "coupon_value": {
            "type": "number",
            "description": "The discount value — percentage number (e.g. 10 for 10%) or dollar amount (e.g. 5 for $5 off)"
          },
          "description": {
            "type": "string",
            "description": "What the coupon says, as spoken by the caller, e.g. 10 percent off any order, 5 dollars off, free delivery"
          }
        },
        "required": ["cart_id", "coupon_type", "coupon_value", "description"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/apply-coupon" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

# ─────────────────────────────────────────────
# TOOL 14 — remove_coupon
# ─────────────────────────────────────────────
echo "14/14 — remove_coupon"
curl -s -X POST https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "function",
    "function": {
      "name": "remove_coupon",
      "description": "Remove the coupon from the cart and restore full pricing. Use if the caller says the coupon does not apply or wants to cancel it.",
      "parameters": {
        "type": "object",
        "properties": {
          "cart_id": {
            "type": "integer",
            "description": "Cart ID to remove the coupon from"
          }
        },
        "required": ["cart_id"]
      }
    },
    "server": { "url": "'"$BASE_URL"'/remove-coupon" }
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print('  OK — id:', r.get('id','ERROR'), r.get('message',''))"

echo ""
echo "Done! All 14 tools registered."
echo "Next: Go to https://dashboard.vapi.ai and attach these tools to your assistant."
