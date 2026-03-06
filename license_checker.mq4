//+------------------------------------------------------------------+
//|  License Checker — Forex SaaS                                    |
//|  Drop this include file into your EA's OnInit()                  |
//+------------------------------------------------------------------+
#property strict

// ── Configuration ────────────────────────────────────────────────
// Replace with your Render URL (no trailing slash)
#define LICENSE_SERVER "https://YOUR-APP.onrender.com"

// Your EA's license key — can be set in EA inputs instead
input string LicenseKey = "ENTER-YOUR-KEY-HERE";

//+------------------------------------------------------------------+
//| Check license against server. Returns true if valid.            |
//+------------------------------------------------------------------+
bool CheckLicense(string key)
{
   string account = IntegerToString(AccountNumber());

   // Build JSON body
   string body = "{\"license\":\"" + key + "\",\"account\":\"" + account + "\"}";

   char   post[];
   char   result[];
   string headers = "Content-Type: application/json\r\n";

   StringToCharArray(body, post, 0, StringLen(body));

   ResetLastError();

   int res = WebRequest(
      "POST",
      LICENSE_SERVER + "/license/check",
      headers,
      5000,          // timeout ms
      post,
      result,
      headers        // response headers (reuse var)
   );

   if (res == -1)
   {
      int err = GetLastError();
      // ERR_WEBREQUEST_INVALID_ADDRESS = 4060
      // Add LICENSE_SERVER to Tools > Options > Expert Advisors > Allowed URLs
      Print("[License] WebRequest failed. Error: ", err,
            " — Make sure '", LICENSE_SERVER, "' is in allowed URLs.");
      return false;
   }

   string response = CharArrayToString(result);
   Print("[License] Server response: ", response);

   // Simple check: server returns {"valid":true,...}
   if (StringFind(response, "\"valid\":true") >= 0)
   {
      // Optionally extract days_remaining
      int dPos = StringFind(response, "days_remaining");
      if (dPos >= 0)
      {
         string sub = StringSubstr(response, dPos + 16, 4);
         Print("[License] Days remaining: ", sub);
      }
      return true;
   }

   // Report specific reason to user
   if      (StringFind(response, "expired")              >= 0) Alert("License EXPIRED. Please renew your subscription.");
   else if (StringFind(response, "bound_to_other_account") >= 0) Alert("License is bound to a different MT4 account.");
   else if (StringFind(response, "not_found")            >= 0) Alert("License key not found. Check your key.");
   else                                                         Alert("License invalid. Contact support.");

   return false;
}

//+------------------------------------------------------------------+
//| Call this inside your EA's OnInit()                              |
//+------------------------------------------------------------------+
int OnLicenseInit()
{
   if (LicenseKey == "" || LicenseKey == "ENTER-YOUR-KEY-HERE")
   {
      Alert("Please enter your license key in EA settings.");
      return INIT_FAILED;
   }

   if (!CheckLicense(LicenseKey))
      return INIT_FAILED;

   Print("[License] ✓ Valid license accepted.");
   return INIT_SUCCEEDED;
}
