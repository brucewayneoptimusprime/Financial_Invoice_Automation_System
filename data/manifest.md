# Extraction answer key

Notes for humans go anywhere in this file. Each invoice has a `##` heading with its exact file name and ONE fenced
`expected` JSON block. Omitted fields are not scored; an explicit `null` means "must be absent". Only entries with
`"verified": true` are scored. `--draft-manifest` writes drafts with `"verified": false`; check every value against the
document yourself, then change it to `true`.

## invoice_Bill Eplett_14021.pdf
- draft extracted by claude-sonnet-5 (prompt extract-v3): CHECK EVERY VALUE against the document, then set "verified" to true
```expected
{
  "verified": true,
  "vendor_name": "SuperStore",
  "vendor_tax_id": null,
  "vendor_address": null,
  "document_type": "invoice",
  "invoice_number": "14021",
  "invoice_date": "2013-03-07",
  "currency": "USD",
  "po_reference": null,
  "subtotal": "9466.50",
  "tax": null,
  "total": "9671.51",
  "line_items": [
    {
      "description": "Canon Wireless Fax, Laser - Copiers, Technology, TEC-CO-3710",
      "quantity": "5",
      "unit_price": "1893.30",
      "amount": "9466.50"
    }
  ],
  "adjustments": [
    {
      "kind": "shipping",
      "amount": "205.01",
      "description": "Shipping"
    }
  ]
}
```

## invoice_Darren Koutras_6459.pdf
- draft extracted by claude-sonnet-5 (prompt extract-v3): CHECK EVERY VALUE against the document, then set "verified" to true
```expected
{
  "verified": true,
  "vendor_name": "SuperStore",
  "vendor_tax_id": null,
  "vendor_address": null,
  "document_type": "invoice",
  "invoice_number": "6459",
  "invoice_date": "2013-03-06",
  "currency": "USD",
  "po_reference": null,
  "subtotal": "9515.00",
  "tax": null,
  "total": "9758.79",
  "line_items": [
    {
      "description": "KitchenAid Stove, Silver (Appliances, Office Supplies)",
      "quantity": "5",
      "unit_price": "1903.00",
      "amount": "9515.00"
    }
  ],
  "adjustments": [
    {
      "kind": "shipping",
      "amount": "243.79",
      "description": "Shipping"
    }
  ]
}
```

## invoice_Liz Thompson_14130.pdf
- draft extracted by claude-sonnet-5 (prompt extract-v3): CHECK EVERY VALUE against the document, then set "verified" to true
```expected
{
  "verified": true,
  "vendor_name": "SuperStore",
  "vendor_tax_id": null,
  "vendor_address": null,
  "document_type": "invoice",
  "invoice_number": "14130",
  "invoice_date": "2013-03-07",
  "currency": "USD",
  "po_reference": null,
  "subtotal": "7556.98",
  "tax": null,
  "total": "6890.46",
  "line_items": [
    {
      "description": "Safco 3-Shelf Cabinet, Mobile - Bookcases, Furniture, FUR-BO-5746",
      "quantity": "7",
      "unit_price": "1079.57",
      "amount": "7556.98"
    }
  ],
  "adjustments": [
    {
      "kind": "discount",
      "amount": "-755.70",
      "description": "Discount (10%)"
    },
    {
      "kind": "shipping",
      "amount": "89.18",
      "description": "Shipping"
    }
  ]
}
```

## invoice_Maria Zettner_24429.pdf
- draft extracted by claude-sonnet-5 (prompt extract-v3): CHECK EVERY VALUE against the document, then set "verified" to true
```expected
{
  "verified": true,
  "vendor_name": "SuperStore",
  "vendor_tax_id": null,
  "vendor_address": null,
  "document_type": "invoice",
  "invoice_number": "24429",
  "invoice_date": "2013-03-07",
  "currency": "USD",
  "po_reference": null,
  "subtotal": "1845.94",
  "tax": null,
  "total": "1770.61",
  "line_items": [
    {
      "description": "Hon Rocking Chair, Black",
      "quantity": "4",
      "unit_price": "461.48",
      "amount": "1845.94"
    }
  ],
  "adjustments": [
    {
      "kind": "discount",
      "amount": "-184.59",
      "description": "Discount (10%)"
    },
    {
      "kind": "shipping",
      "amount": "109.26",
      "description": "Shipping"
    }
  ]
}
```

## invoice_Scot Wooten_10963.pdf
- draft extracted by claude-sonnet-5 (prompt extract-v3): CHECK EVERY VALUE against the document, then set "verified" to true
```expected
{
  "verified": true,
  "vendor_name": "SuperStore",
  "vendor_tax_id": null,
  "vendor_address": null,
  "document_type": "invoice",
  "invoice_number": "10963",
  "invoice_date": "2013-03-07",
  "currency": "USD",
  "po_reference": null,
  "subtotal": "5141.76",
  "tax": null,
  "total": "5338.08",
  "line_items": [
    {
      "description": "Hewlett Fax Machine, Color - Copiers, Technology",
      "quantity": "4",
      "unit_price": "1285.44",
      "amount": "5141.76"
    }
  ],
  "adjustments": [
    {
      "kind": "shipping",
      "amount": "196.32",
      "description": "Shipping"
    }
  ]
}
```
