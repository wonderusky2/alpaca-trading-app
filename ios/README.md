# AlpacaAgent iOS App

Native SwiftUI app. Chat interface, live stats bar, auto-executes trades.

## Xcode Setup (one-time)

1. Open Xcode → **File → New → Project**
2. Choose **iOS → App**
3. Product Name: `AlpacaAgent`
4. Team: your Apple ID (free personal team works for device install)
5. Bundle ID: `com.johnshelest.AlpacaAgent`
6. Interface: **SwiftUI**, Language: **Swift**
7. **Delete** the generated `ContentView.swift` and `[AppName]App.swift`
8. Drag all `.swift` files from this folder into the Xcode project (copy if asked)
9. Replace the generated `Info.plist` entries with the ones in `Info.plist` here
   — or just add the `NSAppTransportSecurity` block to the existing plist

## Config

Edit `Config.swift` before building:
- `serverURL` — already set to `http://34.60.235.98:5001`
- `apiKey`    — set to the key from your K8s secret (`LAB_API_KEY` value)

## Install on iPhone

1. Plug iPhone into Mac via USB
2. In Xcode, select your iPhone as the run destination
3. Product → Run (⌘R)
4. First time: on iPhone go to **Settings → General → VPN & Device Management** → trust your developer certificate

## Auto-Execute

Trades are executed immediately — no confirmation prompt. The chat shows "Executing: BUY N SYMBOL…" then "✓ Executed" or the error.

## Accessing from iPhone on Cellular

The server LoadBalancer currently allows only `209.122.50.243/32`.
To allow iPhone access, run:

```bash
# In robinhood-trader repo:
kubectl patch service alpaca-server -n alpaca-trader \
  --type=json \
  -p='[{"op":"remove","path":"/spec/loadBalancerSourceRanges"}]'
```

This opens the port to all IPs. The `X-API-Key` header check in the app + server
prevents unauthorized access.
