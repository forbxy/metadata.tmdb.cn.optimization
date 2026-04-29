# coding: utf-8
import json
import urllib.request
import urllib.error
import xbmc
import xbmcgui
import xbmcaddon

ADDON_SETTINGS = xbmcaddon.Addon()
icon_path = ADDON_SETTINGS.getAddonInfo('icon')

class DeepSeekExtractor:
    def __init__(self, api_key, base_url, model, prompt_template):
        self.api_key = api_key
        if base_url.endswith("/v1"):
            self.base_url = base_url
        elif base_url.endswith("/"):
            self.base_url = base_url.rstrip('/')
        else:
            self.base_url = base_url

        self.model = model
        self.prompt_template = prompt_template

    def extract(self, filename):
        if not self.api_key:
            xbmc.log("[DeepSeek] API Key is missing", xbmc.LOGWARNING)
            return None

        # Handle different base URL styles if needed, but standard deepseek is https://api.deepseek.com
        # Completion endpoint: https://api.deepseek.com/chat/completions
        url = f"{self.base_url}/chat/completions"
        
        # Build prompt. The word "json" must appear in the prompt for response_format to work.
        content_prompt = f"{self.prompt_template}\n文件名: {filename}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": content_prompt}
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 256,
            "stream": False,
            "thinking": {"type": "disabled"}
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json"
        }

        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                resp_data = response.read()
                resp_json = json.loads(resp_data)
                
                # Check for errors in response
                if 'error' in resp_json:
                    xbmc.log(f"[DeepSeek] API returned error: {resp_json['error']}", xbmc.LOGERROR)
                    return None

                content = resp_json['choices'][0]['message']['content']
                xbmc.log(f"[DeepSeek] Raw response for {filename}: {content}", xbmc.LOGDEBUG)

                data = json.loads(content)
                return data

        except json.JSONDecodeError as e:
            xbmc.log(f"[DeepSeek] JSON decode failed: {e} {resp_data}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("TMDB CN Optimization", "DeepSeek 返回了非 JSON 数据", icon_path, 3000)
        except Exception as e:
            xbmc.log(f"[DeepSeek] Request Error: {e}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("TMDB CN Optimization", f"DeepSeek 请求错误: {e}", icon_path, 3000)
        return None
