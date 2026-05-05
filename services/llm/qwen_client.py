import os
import requests


class QwenClient:
    """Qwen 客户端：支持 HTTP 推理和本地模型直连两种模式。"""

    def __init__(self):
        self.base_url = os.getenv('QWEN_API_URL', 'http://127.0.0.1:8008/generate')
        self.enabled = os.getenv('QWEN_ENABLED', '1') == '1'
        self.model_path = os.getenv('QWEN_MODEL_PATH', '')
        self.load_at_startup = os.getenv('QWEN_LOAD_AT_STARTUP', '1') == '1'
        self.backend = 'http'
        self._tokenizer = None
        self._model = None

        if self.enabled and self.model_path and self.load_at_startup:
            self._load_local_model()

    def _load_local_model(self):
        """启动时加载本地模型，避免首次问答延迟。"""
        try:
            from transformers import AutoTokenizer, AutoModelForCausalLM
            import torch

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=True)
            dtype = torch.float16 if torch.cuda.is_available() else torch.float32
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                trust_remote_code=True,
                torch_dtype=dtype,
                device_map='auto' if torch.cuda.is_available() else None,
            )
            self.backend = 'local'
            print(f"✅ Qwen 本地模型已加载: {self.model_path}")
        except Exception as e:
            self._tokenizer = None
            self._model = None
            self.backend = 'http'
            print(f"⚠️ Qwen 本地模型加载失败，回退 HTTP 模式: {e}")

    def warmup(self):
        """应用启动时主动预热。"""
        if self.enabled and self.model_path and self._model is None:
            self._load_local_model()

    def _generate_local(self, prompt):
        inputs = self._tokenizer(prompt, return_tensors='pt')
        if hasattr(self._model, 'device'):
            inputs = {k: v.to(self._model.device) for k, v in inputs.items()}

        outputs = self._model.generate(
            **inputs,
            max_new_tokens=256,
            do_sample=True,
            temperature=0.2,
            top_p=0.9,
            pad_token_id=self._tokenizer.eos_token_id,
        )
        text = self._tokenizer.decode(outputs[0], skip_special_tokens=True)
        return text[len(prompt):].strip() if text.startswith(prompt) else text

    def generate(self, prompt):
        if not self.enabled:
            return "【本地客服降级】模型服务未启用。根据知识库结果已返回结构化答案。"

        if self.backend == 'local' and self._model is not None and self._tokenizer is not None:
            try:
                return self._generate_local(prompt)
            except Exception as e:
                print(f"⚠️ 本地模型推理失败，回退 HTTP 模式: {e}")

        try:
            resp = requests.post(
                self.base_url,
                json={"prompt": prompt, "max_tokens": 512, "temperature": 0.2},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get('text') or data.get('answer') or ''
        except Exception as e:
            return f"【智能客服降级】模型不可用，请检查 QWEN_MODEL_PATH/QWEN_API_URL。错误: {e}"
