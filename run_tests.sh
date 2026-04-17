#!/bin/bash

BASE="http://localhost:8000"
PASS=0
FAIL=0

# Unique phone per run — avoids stale-DB failures across test runs
TEST_PHONE="561$(date +%s | tail -c 8)"

check() {
  local label=$1
  local result=$2
  local expect=$3
  if echo "$result" | grep -q "$expect"; then
    echo "✅ PASS — $label"
    PASS=$((PASS + 1))
  else
    echo "❌ FAIL — $label"
    echo "   Expected to find: $expect"
    echo "   Got: $(echo $result | head -c 200)"
    FAIL=$((FAIL + 1))
  fi
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  RESTAURANT AGENT — BACKEND TEST SUITE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Health
R=$(curl -s $BASE/)
check "Health check" "$R" "ok"

# Lookup unknown customer
R=$(curl -s -X POST $BASE/lookup-customer-by-phone \
  -H "Content-Type: application/json" \
  -d "{\"phone_number\":\"$TEST_PHONE\"}")
check "Lookup unknown → found:false" "$R" '"found":false'

# Save new customer
R=$(curl -s -X POST $BASE/save-or-update-customer \
  -H "Content-Type: application/json" \
  -d "{\"phone_number\":\"$TEST_PHONE\",\"first_name\":\"Test\",\"last_name\":\"User\"}")
check "Save new customer → created" "$R" "created"

# Lookup returning customer
R=$(curl -s -X POST $BASE/lookup-customer-by-phone \
  -H "Content-Type: application/json" \
  -d "{\"phone_number\":\"$TEST_PHONE\"}")
check "Lookup returning → found:true" "$R" '"found":true'

# Delivery eligibility — within range (Boca Raton, close to restaurant default coords)
R=$(curl -s -X POST $BASE/check-delivery-eligibility \
  -H "Content-Type: application/json" \
  -d '{"address":"700 Yamato Rd, Boca Raton FL 33431"}')
check "Delivery eligibility — within range" "$R" '"eligible":true'

# Create pickup cart
R=$(curl -s -X POST $BASE/create-order-cart \
  -H "Content-Type: application/json" \
  -d "{\"phone_number\":\"$TEST_PHONE\",\"customer_name\":\"Test User\",\"order_type\":\"pickup\"}")
check "Create pickup cart" "$R" "cart_id"
CART_ID=$(echo $R | python3 -c "import sys,json; print(json.load(sys.stdin)['cart_id'])" 2>/dev/null)

# Add items
R=$(curl -s -X POST $BASE/add-item-to-cart \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART_ID,\"item_name\":\"Cheese Pizza\",\"quantity\":1,\"unit_price\":14.99,\"size\":\"large\"}")
check "Add item 1 to cart" "$R" "line_total"
ITEM_ID=$(echo $R | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

R=$(curl -s -X POST $BASE/add-item-to-cart \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART_ID,\"item_name\":\"Garlic Knots\",\"quantity\":1,\"unit_price\":6.99}")
check "Add item 2 to cart" "$R" "line_total"
ITEM_ID2=$(echo $R | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

# Get cart summary
R=$(curl -s -X POST $BASE/get-cart-summary \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART_ID}")
check "Cart summary — 2 items" "$R" '"item_count":2'
check "Cart summary — food subtotal" "$R" "food_subtotal"

# Update item quantity
R=$(curl -s -X POST $BASE/update-cart-item \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART_ID,\"cart_item_id\":$ITEM_ID,\"quantity\":2}")
check "Update item quantity" "$R" "updated"

# Remove item
R=$(curl -s -X POST $BASE/remove-cart-item \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART_ID,\"cart_item_id\":$ITEM_ID2}")
check "Remove item from cart" "$R" "removed"

# Confirm pickup order
R=$(curl -s -X POST $BASE/confirm-order \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART_ID}")
check "Confirm pickup order" "$R" "confirmed"

# Create delivery cart — below minimum
R=$(curl -s -X POST $BASE/create-order-cart \
  -H "Content-Type: application/json" \
  -d "{\"phone_number\":\"$TEST_PHONE\",\"customer_name\":\"Test User\",\"order_type\":\"delivery\",\"delivery_address\":\"100 SE 5th Ave, Boca Raton FL\"}")
CART2=$(echo $R | python3 -c "import sys,json; print(json.load(sys.stdin)['cart_id'])" 2>/dev/null)

R=$(curl -s -X POST $BASE/add-item-to-cart \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART2,\"item_name\":\"Coke\",\"quantity\":1,\"unit_price\":3.99}")

R=$(curl -s -X POST $BASE/confirm-order \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART2}")
check "Delivery below minimum → rejected" "$R" "422\|minimum\|Minimum"

# Cancel that cart
R=$(curl -s -X POST $BASE/cancel-cart \
  -H "Content-Type: application/json" \
  -d "{\"cart_id\":$CART2}")
check "Cancel cart" "$R" "cancelled"

# Order history
R=$(curl -s -X POST $BASE/lookup-customer-by-phone \
  -H "Content-Type: application/json" \
  -d "{\"phone_number\":\"$TEST_PHONE\"}")
check "Order history in lookup response" "$R" "order_history"
check "Favorite item tracked" "$R" "favorite_item"

# Staff dashboard
R=$(curl -s "$BASE/orders?status=confirmed")
check "Staff dashboard — confirmed orders" "$R" "confirmed"

R=$(curl -s "$BASE/orders?status=cancelled")
check "Staff dashboard — cancelled orders" "$R" "cancelled"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  RESULTS: $PASS passed, $FAIL failed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
