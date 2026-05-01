# coding: utf-8
import json
import time
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

        xbmc.log(f"[DeepSeek] >>> 请求开始 | 模型: {self.model} | API: {self.base_url}", xbmc.LOGINFO)
        xbmc.log(f"[DeepSeek] >>> 原始文件名: {filename}", xbmc.LOGINFO)
        xbmc.log(f"[DeepSeek] >>> 发送Prompt:\n{content_prompt}", xbmc.LOGINFO)

        try:
            t_start = time.time()
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                elapsed = time.time() - t_start
                resp_data = response.read()
                resp_json = json.loads(resp_data)

                xbmc.log(f"[DeepSeek] <<< 响应耗时: {elapsed:.2f}s | HTTP状态: {response.getcode()}", xbmc.LOGINFO)

                # Check for errors in response
                if 'error' in resp_json:
                    xbmc.log(f"[DeepSeek] <<< API返回错误: {resp_json['error']}", xbmc.LOGERROR)
                    return None

                usage = resp_json.get('usage', {})
                if usage:
                    xbmc.log(f"[DeepSeek] <<< Token用量: prompt={usage.get('prompt_tokens', '?')}, "
                             f"completion={usage.get('completion_tokens', '?')}, "
                             f"total={usage.get('total_tokens', '?')}", xbmc.LOGINFO)

                content = resp_json['choices'][0]['message']['content']
                xbmc.log(f"[DeepSeek] <<< 原始响应: {content}", xbmc.LOGINFO)

                data = json.loads(content)
                xbmc.log(f"[DeepSeek] <<< 解析结果: cn={data.get('cn', data.get('chinese', data.get('zh', 'N/A')))}, "
                         f"en={data.get('en', data.get('english', data.get('englist', 'N/A')))}, "
                         f"year={data.get('year', data.get('yr', 'N/A'))}", xbmc.LOGINFO)
                return data

        except json.JSONDecodeError as e:
            xbmc.log(f"[DeepSeek] <<< JSON解析失败: {e} | 原始数据: {resp_data}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("TMDB CN Optimization", "DeepSeek 返回了非 JSON 数据", icon_path, 3000)
        except Exception as e:
            xbmc.log(f"[DeepSeek] <<< 请求异常: {e}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("TMDB CN Optimization", f"DeepSeek 请求错误: {e}", icon_path, 3000)
        return None
