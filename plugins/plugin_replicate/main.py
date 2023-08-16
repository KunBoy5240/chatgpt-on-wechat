# encoding:utf-8
import json
import os
import langid
from bridge.bridge import Bridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from config import conf
import plugins
from plugins import *
from common.log import logger
import replicate
from common.expired_dict import ExpiredDict

@plugins.register(name="replicate", desc="利用replicate api来画图", version="0.3", author="lanvent")
class Replicate(Plugin):
    def __init__(self):
        super().__init__()
        curdir = os.path.dirname(__file__)
        config_path = os.path.join(curdir, "config.json")
        self.params_cache = ExpiredDict(60 * 60)
        if not os.path.exists(config_path):
            logger.info('[RP] 配置文件不存在，将使用config-template.json模板')
            config_path = os.path.join(curdir, "config.json.template")
        try:
            self.apitoken = None
            if os.environ.get("replicate_api_token", None):
                self.apitoken = os.environ.get("replicate_api_token")
            if os.environ.get("replicate_api_token".upper(), None):
                self.apitoken = os.environ.get("replicate_api_token".upper())

            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                self.rules = config["rules"]
                self.default_params = config["defaults"]
                if not self.apitoken:
                    self.apitoken = config["replicate_api_token"]
                if self.apitoken == "YOUR API TOKEN":
                    raise Exception("please set your api token in config or environment variable.")
                self.client = replicate.Client(self.apitoken)
                self.translate_prompt = config.get("translate_prompt", False)
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            logger.info("[RP] inited")
        except Exception as e:
            if isinstance(e, FileNotFoundError):
                logger.warn(f"[RP] init failed, config.json not found.")
            else:
                logger.warn("[RP] init failed." + str(e))
            raise e
    
    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type not in [ContextType.IMAGE_CREATE, ContextType.IMAGE]:
            return

        logger.debug("[RP] on_handle_context. content: %s" %e_context['context'].content)

        logger.info("[RP] image_query={}".format(e_context['context'].content))
        reply = Reply()
        try:
            user_id = e_context['context']["session_id"]
            content = e_context['context'].content[:]
            if e_context['context'].type == ContextType.IMAGE_CREATE:
                # 解析用户输入 如"横版 高清 二次元:cat"
                content = content.replace("，", ",").replace("：", ":")
                if ":" in content:
                    keywords, prompt = content.split(":", 1)
                else:
                    keywords = content
                    prompt = ""

                keywords = keywords.split()
                unused_keywords = []
                if "help" in keywords or "帮助" in keywords:
                    reply.type = ReplyType.INFO
                    reply.content = self.get_help_text(verbose = True)
                else:
                    rule_params = {}
                    for keyword in keywords:
                        matched = False
                        for rule in self.rules:
                            if keyword in rule["keywords"]:
                                for key in rule["params"]:
                                    rule_params[key] = rule["params"][key]
                                matched = True
                                break  # 一个关键词只匹配一个规则
                        if not matched:
                            unused_keywords.append(keyword)
                            # logger.info("[RP] keyword not matched: %s, add to prompt" % keyword)
                            logger.info("[RP] keyword not matched: %s, exit" % keyword)
                            return
                    params = {**self.default_params, **rule_params}
                    params["prompt"] = params.get("prompt", "")
                    if unused_keywords:
                        if prompt:
                            prompt += f", {', '.join(unused_keywords)}"
                        else:
                            prompt = ', '.join(unused_keywords)
                    if prompt:
                        if self.translate_prompt:
                            lang = langid.classify(prompt)[0]
                            if lang != "en" and len(unused_keywords) == 0:
                                logger.info("[RP] translate prompt from {} to en".format(lang))
                                try:
                                    prompt = Bridge().fetch_translate(prompt, to_lang= "en")
                                except Exception as e:
                                    logger.info("[RP] translate failed: {}".format(e))
                                logger.info("[RP] translated prompt={}".format(prompt))
                        params["prompt"] += f", {prompt}"
                    logger.info("[RP] params={}".format(params))

                    if params.get("model",None) is None or params.get("version",None) is None:
                        logger.info("[RP] model or version not set, exit")
                        return

                    if params.get("image",None):
                        self.params_cache[user_id] = params
                        reply.type = ReplyType.INFO
                        reply.content = "请发送一张图片给我"
                    else:
                        model = self.client.models.get(params.pop("model"))
                        version = model.versions.get(params.pop("version"))
                        if "_model" in params:
                            params["model"] = params.pop("_model")
                        if "_version" in params:
                            params["version"] = params.pop("_version")
                        result = version.predict(**params)
                        if isinstance(result, list):
                            result = result[-1]
                        reply.type = ReplyType.IMAGE_URL
                        reply.content = result
                    e_context.action = EventAction.BREAK_PASS  # 事件结束后，跳过处理context的默认逻辑
                    e_context['reply'] = reply
            else:
                cmsg = e_context['context']['msg']
                if user_id in self.params_cache:
                    params = self.params_cache[user_id]
                    del self.params_cache[user_id]
                    cmsg.prepare()
                    img_key = params.pop("image")
                    params[img_key]=open(content,"rb")
                    model = self.client.models.get(params.pop("model"))
                    version = model.versions.get(params.pop("version"))
                    if "_model" in params:
                        params["model"] = params.pop("_model")
                    if "_version" in params:
                        params["version"] = params.pop("_version")
                    result = version.predict(**params)
                    if isinstance(result, list):
                        result = result[-1]
                    reply.type = ReplyType.IMAGE_URL
                    reply.content = result
                    logger.info("[RP] result={}".format(result))
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS  # 事件结束后，跳过处理context的默认逻辑

        except Exception as e:
            reply.type = ReplyType.ERROR
            reply.content = "[RP] "+str(e)
            e_context['reply'] = reply
            logger.exception("[RP] exception: %s" % e)
            e_context.action = EventAction.CONTINUE  # 事件继续，交付给下个插件或默认逻辑

    def get_help_text(self, verbose = False, **kwargs):
        if not conf().get('image_create_prefix'):
            return "画图功能未启用"
        else:
            trigger = conf()['image_create_prefix'][0]
        help_text = "利用replicate api来画图。\n"
        if not verbose:
            return help_text
        
        help_text += f"使用方法:\n使用\"{trigger}[关键词1] [关键词2]...:提示语\"的格式作画，如\"{trigger}竖版:girl\"\n"
        help_text += "目前可用关键词：\n"
        for rule in self.rules:
            keywords = [f"[{keyword}]" for keyword in rule['keywords']]
            help_text += f"{','.join(keywords)}"
            if "desc" in rule:
                help_text += f"-{rule['desc']}\n"
            else:
                help_text += "\n"
        return help_text

