#!/bin/bash

VAPI_KEY="${VAPI_KEY:-your-vapi-api-key-here}"

# The 14 active tool IDs — keep these, delete everything else
KEEP=(
  "f81a56ec-33f1-49c9-9267-d13ced896764"  # lookup_customer_by_phone
  "fa3dca36-22d7-48f8-a774-0074b848b5b6"  # save_or_update_customer
  "97097123-c220-4ac8-a00c-0ed53ee79d0d"  # create_order_cart
  "8cab209b-8739-430c-a8f2-9fa0a94c35bc"  # add_item_to_cart
  "e0bb8aec-e797-4bae-882e-0c29046a6916"  # get_cart_summary
  "8d166fe5-3595-46f1-8e0a-bb0bc80e6243"  # update_cart_item
  "d7d758e0-f49a-4a33-9df4-90b08fde3acd"  # remove_cart_item
  "fff46bb2-9fb1-4022-8148-76fff592559f"  # clear_cart
  "30de2359-e5a7-4c06-b932-35a70df70876"  # cancel_cart
  "65c19140-9019-4b5d-bba5-2cf67cfd0ec3"  # confirm_order
  "72f56b1c-519f-4860-9b9a-d7f4d10ade38"  # set_order_time
  "50726d97-698f-4c50-815b-3982d56b3e6e"  # apply_coupon
  "ede099c8-64ca-4d7d-85bb-2db53118c33b"  # remove_coupon
  "d3eefcb1-dd04-4cdb-9d71-04f97d5b3a85"  # check_store_status
)

echo "Fetching all tools from Vapi..."
ALL_TOOLS=$(curl -s -X GET https://api.vapi.ai/tool \
  -H "Authorization: Bearer $VAPI_KEY" \
  -H "Content-Type: application/json")

# Extract all IDs
ALL_IDS=$(echo "$ALL_TOOLS" | python3 -c "
import sys, json
tools = json.load(sys.stdin)
for t in tools:
    print(t['id'])
")

KEEP_SET=" ${KEEP[*]} "
DELETED=0
SKIPPED=0

for id in $ALL_IDS; do
  if [[ $KEEP_SET == *" $id "* ]]; then
    echo "  KEEP    $id"
    SKIPPED=$((SKIPPED + 1))
  else
    NAME=$(echo "$ALL_TOOLS" | python3 -c "
import sys, json
tools = json.load(sys.stdin)
for t in tools:
    if t['id'] == '$id':
        print(t.get('function',{}).get('name','?'))
" 2>/dev/null)
    curl -s -X DELETE "https://api.vapi.ai/tool/$id" \
      -H "Authorization: Bearer $VAPI_KEY" > /dev/null
    echo "  DELETED $id  ($NAME)"
    DELETED=$((DELETED + 1))
  fi
done

echo ""
echo "Done — kept $SKIPPED tools, deleted $DELETED old tools."
