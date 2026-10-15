# Run as Administrator: allows the Mega Rhexas dashboards (TCP 8080-8092) from the home LAN only (Private profile, local subnet).
# The port list is the one in rhexas.toml [dashboard.sectors]. Update both together if a port changes.
Remove-NetFirewallRule -DisplayName "Mega Rhexas Dashboard" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Mega Rhexas Dashboard" -Direction Inbound -Protocol TCP -LocalPort 8080-8092 -RemoteAddress LocalSubnet -Profile Private -Action Allow -ErrorAction Stop | Out-Null
Write-Host "Firewall rule 'Mega Rhexas Dashboard' added (TCP 8080-8092, LocalSubnet, Private)."

