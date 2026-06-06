# `3d procurement`

Builds a deterministic local purchase plan from a BOM file and an inventory file.
It does not call supplier APIs, scrape prices, or use the network; supplier names,
package quantities, and quantities all come from the input documents.

## Usage

```bash
3d procurement plan --bom <file> --inventory <file> [--format table|json]
```

`--bom` is the required parts/materials demand file. `--inventory` is the required
available-stock file. Both inputs may be JSON or YAML and may use either an `items:`
list or a compact `items:` mapping.

## Examples

Human-readable shortage table:

```bash
3d procurement plan --bom bom.yaml --inventory inventory.yaml
```

Machine-readable plan:

```bash
3d procurement plan --bom bom.json --inventory inventory.json --format json
```

Shell pipeline that reports only the SKUs an agent needs to buy:

```bash
3d procurement plan --bom bom.json --inventory inventory.json --format json | jq -r '.items[].sku'
```

## Input Shape

Verbose list:

```yaml
items:
  - sku: m3-bolt
    description: M3 bolt
    quantity: 24
    unit: each
    supplier: BoltCo
    package_qty: 50
```

Compact mapping:

```yaml
items:
  m3-bolt: 24
```

Duplicate SKUs are combined before the shortage is calculated. If `package_qty` is
present, the buy quantity rounds up to the next package.
