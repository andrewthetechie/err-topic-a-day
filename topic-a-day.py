import logging
import random
import secrets
from datetime import datetime
from hashlib import sha256
from io import StringIO
from threading import RLock
from typing import Any
from typing import Dict
from typing import List

import requests
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from decouple import config as get_config
from errbot import arg_botcmd
from errbot import botcmd
from errbot import BotPlugin
from errbot import webhook
from errbot.backends.base import Message as ErrbotMessage
from flask import abort
from wrapt import synchronized  # https://stackoverflow.com/a/29403915

TOPICS_LOCK = RLock()


def do_webhook_post(url, headers={}, data={}):
    logger = logging.getLogger()
    logger.debug("Starting request for %s with headers: %s", url, headers)
    try:
        response = requests.post(url, headers=headers, data=data)
    except Exception as err:
        logger.error("Error while doing request: %s", err)
        return
    logger.debug("Webhook status code: %s", response.status_code)
    logger.debug("Webhook response: %s", response.text)


def get_config_item(
    key: str, config: Dict, overwrite: bool = False, **decouple_kwargs
) -> Any:
    """
    Checks config to see if key was passed in, if not gets it from the environment/config file

    If key is already in config and overwrite is not true, nothing is done. Otherwise, config var is added to config
    at key
    """
    if key not in config and not overwrite:
        config[key] = get_config(key, **decouple_kwargs)


class Topics:
    """
    Topics are our topics that we want to post. This is basically a big wrapper class around a python list of
    dicts that uses the Errbot Storage engine to store itself.

    In the future, I'd like to see this replaced by some sort of database engine to make this more robust. This
    is good enough for a MVP.
    """

    def __init__(self, bot_plugin: BotPlugin) -> None:
        self.bot_plugin = bot_plugin
        try:
            self.bot_plugin["TOPICS"]
        except KeyError:
            # this is the first time this plugin is starting up
            self.bot_plugin["TOPICS"] = []

    @synchronized(TOPICS_LOCK)
    def add(self, topic: str) -> None:
        """
        Adds a new topic to the list. Does not check for duplicates

        """
        topics = self.bot_plugin["TOPICS"]
        topics.append(
            {
                "id": self.hash_topic(topic),
                "topic": topic,
                "used": False,
                "used_date": None,
            }
        )
        self.bot_plugin["TOPICS"] = topics

    def get_random(self) -> Dict:
        """
        Returns a random, unused topic
        """
        try:
            return random.choice(  # nosec
                list(filter(lambda d: not d["used"], self.bot_plugin["TOPICS"]))
            )
        except IndexError:
            self.bot_plugin.log.error("Topic list was empty when trying to get a topic")
            raise self.NoNewTopicsError("No new topics")

    def list(self) -> List[Dict]:
        """
        Returns the topic list

        """
        return self.bot_plugin["TOPICS"]

    @synchronized(TOPICS_LOCK)
    def set_used(self, topic_id: str) -> None:
        """
        Sets 'used' to true and 'used_date' to datetime.now for the topic 'id' = topic_id

        This indicates a topic has been posted
        """
        found = False
        topics = self.bot_plugin["TOPICS"]
        for topic in topics:
            if topic["id"] == topic_id:
                topic["used"] = True
                topic["used_date"] = datetime.now()
                found = True
        if not found:
            raise KeyError(f"{topic_id} not found in topic list")
        self.bot_plugin["TOPICS"] = topics

    @synchronized(TOPICS_LOCK)
    def delete(self, topic_id: str) -> None:
        """
        Deletes the topic at topic_id

        topic_id should be the 8 character topic hash from id in the topic
        """
        found = False
        topics = self.bot_plugin["TOPICS"]
        for index, topic in enumerate(topics):
            if topic["id"] == topic_id:
                found = True
                to_pop = index
                break
        if not found:
            raise KeyError(f"{topic_id} not found in topic list")
        topics.pop(to_pop)
        self.bot_plugin["TOPICS"] = topics

    @synchronized(TOPICS_LOCK)
    def reset(self, topic_id: str) -> None:
        """
        resets the topic at topic_id

        topic_id should be the 8 character topic hash from id in the topic
        """
        found = False
        topics = self.bot_plugin["TOPICS"]
        for index, topic in enumerate(topics):
            if topic["id"] == topic_id:
                found = True
                topic["used"] = False
                topic["used_date"] = None
                break
        if not found:
            raise KeyError(f"{topic_id} not found in topic list")
        self.bot_plugin["TOPICS"] = topics

    @staticmethod
    def hash_topic(topic: str) -> str:
        """
        Returns an 8 character id hash of a topic with the current datetime (for uniqueness)
        """
        return sha256(f"{topic}-{datetime.now()}".encode("utf-8")).hexdigest()[:8]

    class NoNewTopicsError(Exception):
        pass


class TopicADay(BotPlugin):
    """Manages a topic a day channel for a slack group"""

    def __init__(self, bot, name: str = None) -> None:
        """
        Calls super init and adds a few plugin variables of our own. This makes PEP8 happy
        """
        super().__init__(bot, name)
        self.log.debug("Done with init")

    # botplugin methods, these are not commands and just configure/setup our plugin
    def activate(self) -> None:
        """
        Activates the plugin. Schedules our jobs and starts our poller to run them
        """
        super().activate()
        self.topics = Topics(self)
        # schedule our daily jobs
        self.sched = BackgroundScheduler(self.config["TAD_APSCHEDULER_CONFIG"])
        try:
            if self.config["TAD_ENABLE_WEBHOOK"]:
                request_args = {"url": self.config["TAD_WEBHOOK_URL"]}
                if self.config["AUTH_POST_WEBHOOK"]:
                    request_args["headers"] = {
                        "x-auth-token": self.config["AUTH_POST_WEBHOOK_TOKEN"]
                    }
                self.sched.add_job(
                    do_webhook_post,
                    CronTrigger.from_crontab(self.config["TAD_SCHEDULE"]),
                    kwargs=request_args,
                    name="topic-a-day",
                    id="topic-a-day",
                    replace_existing=True,
                )
            else:
                self.sched.add_job(
                    self.post_topic,
                    CronTrigger.from_crontab(self.config["TAD_SCHEDULE"]),
                    name="topic-a-day",
                    id="topic-a-day",
                    replace_existing=True,
                )
        except ConflictingIdError as err:
            self.log.debug("Hit error when adding job: %s", err)
        except Exception as err:
            self.log.error("Hit error while adding job: %s", err)
        self.sched.start()

    def deactivate(self) -> None:
        """
        Shutsdown the scheduler and calls super deactivate
        """
        self.sched.shutdown()
        super().deactivate()

    def configure(self, configuration: Dict) -> None:
        """
        Configures the plugin
        """
        self.log.debug("Starting Config")
        if configuration is None:
            configuration = dict()

        # name of the channel to post in
        get_config_item("TAD_CHANNEL", configuration)
        if getattr(self._bot, "channelname_to_channelid", None) is not None:
            configuration["TOPIC_CHANNEL_ID"] = self._bot.channelname_to_channelid(
                configuration["TAD_CHANNEL"]
            )
        get_config_item("TAD_SCHEDULE", configuration, default="0 9 * * 1,3,5")

        # apscheduler config
        get_config_item("TAD_APSCHEDULER_CONFIG_FILE", configuration, default="")
        if configuration["TAD_APSCHEDULER_CONFIG_FILE"] != "":
            configuration["TAD_APSCHEDULER_CONFIG"] = self._load_config_file(
                configuration["TAD_APSCHEDULER_CONFIG_FILE"]
            )
        else:
            get_config_item("TOPIC_TZ", configuration, default="UTC")
            configuration["TAD_APSCHEDULER_CONFIG"] = {
                "apscheduler.timezone": configuration["TOPIC_TZ"]
            }

        # Webhook options
        get_config_item("TAD_ENABLE_WEBHOOK", configuration, default="False", cast=bool)
        if configuration["TAD_ENABLE_WEBHOOK"]:
            get_config_item(
                "TAD_WEBHOOK_URL",
                configuration,
                default="http://localhost:3142/post_topic_rpc",
            )
            get_config_item(
                "AUTH_POST_WEBHOOK", configuration, default="True", cast=bool
            )
            if configuration["AUTH_POST_WEBHOOK"]:
                get_config_item(
                    "AUTH_POST_WEBHOOK_TOKEN",
                    configuration,
                    default=secrets.token_urlsafe(),
                )
        super().configure(configuration)

    @staticmethod
    def _load_config_file(filepath: str) -> Dict:
        """"""
        import json

        with open(filepath, "r") as config_file:
            data = json.load(config_file)
        return data

    @botcmd
    @arg_botcmd("topic", nargs="*", type=str, help="Topic to add to our topic list")
    def add_topic(self, msg: ErrbotMessage, topic: List[str]) -> None:
        """
        Adds a topic to our topic list for future discussion.
        """
        # topic is a nargs representation of whatever was passed in, lets make it a sentence
        topic_sentence = " ".join(topic)
        # TODO : Implement an admin system with approval for topics
        self.topics.add(topic_sentence)
        self.send(
            msg.frm, f"Topic added to the list: ```{topic_sentence}```", in_reply_to=msg
        )

    @botcmd(admin_only=True)
    @arg_botcmd(
        "topic_id", type=str, help="Hash of the topic to remove from list topics"
    )
    def reset_topic(self, msg: ErrbotMessage, topic_id: str) -> str:
        """
        Resets a topic from the topic list so it can be posted again
        """
        if len(topic_id) != 8:
            return "Invalid Topic ID"

        try:
            self.topics.reset(topic_id)
        except KeyError:
            return "Invalid Topic ID"

        return "Topic Reset"

    @botcmd(admin_only=True)
    @arg_botcmd(
        "topic_id", type=str, help="Hash of the topic to remove from list topics"
    )
    def delete_topic(self, msg: ErrbotMessage, topic_id: str) -> str:
        """
        Deletes a topic from the topic list

        """
        if len(topic_id) != 8:
            return "Invalid Topic ID"

        try:
            self.topics.delete(topic_id)
        except KeyError:
            return "Invalid Topic ID"

        return "Topic Deleted"

    @botcmd
    def list_topics(self, msg: ErrbotMessage, _: List) -> None:
        """
        Lists all of our topics
        """
        topics = self.topics.list()
        used_topics = []
        free_topics = []
        for topic in topics:
            if topic["used"]:
                used_topics.append(
                    f"{topic['id']}: {topic['topic']} -- Posted on {topic['used_date'].strftime('%Y-%m-%d %H:%M')}"
                )
            else:
                free_topics.append(f"{topic['id']}: {topic['topic']}")

        self.send(
            msg.frm,
            "Previously posted topics:\n{}".format("\n".join(used_topics)),
            in_reply_to=msg,
        )
        self.send(
            msg.frm,
            "Upcoming Topics:\n{}".format("\n".join(free_topics)),
            in_reply_to=msg,
        )

    @botcmd(admin_only=True)
    def list_topic_jobs(self, msg: ErrbotMessage, _: List) -> None:
        """
        List the scheduled jobs
        """
        pjobs_out = StringIO()
        self.sched.print_jobs(out=pjobs_out)
        self.send(msg.frm, pjobs_out.getvalue(), in_reply_to=msg)

    @webhook(methods=["POST"], raw=True)
    def post_topic_rpc(self, request):
        if not self.config["TAD_ENABLE_WEBHOOK"]:
            abort(500)
        if self.config["AUTH_POST_WEBHOOK"]:
            if (
                request.headers.get("x-auth-token", "")
                != self.config["AUTH_POST_WEBHOOK_TOKEN"]
            ):
                abort(403, "Endpoint auth turned on and your auth token did not match")

        self.post_topic()
        return "Ok"

    def post_topic(self) -> None:
        """
        Called by our scheduled jobs to post the topic message for the day. Also calls any backend specific
        pre_post_topic methods
        """
        self.log.debug("Calling post_topic")
        try:
            new_topic = self.topics.get_random()
        except Topics.NoNewTopicsError:
            self.log.error("No new topics, cannot post")
            self.warn_admins(
                "There are no new topics for topic a day so today's post failed"
            )
            return
        topic_template = f"Today's Topic: {new_topic['topic']}"
        self.log.debug("Topic template: %s", topic_template)
        # call any special steps for the backend
        try:
            backend_specific = getattr(self, f"{self._bot.mode}_pre_post_topic")
            backend_specific(topic_template)
        except AttributeError:
            self.log.debug("%s has no backend specific tasks", self._bot.mode)
        self.log.debug("Sending message to channel")
        self.send(self.build_identifier(self.config["TAD_CHANNEL"]), topic_template)
        self.log.debug("Setting topic to used")
        self.topics.set_used(new_topic["id"])

    # Backend specific pre_post tasks. Examples include setting channel topics
    # Backend specific pre_post tasks should be named like {backend_name}_pre_post_topic and take two arguments, self
    # and a topic: str. They should not return anything
    def slack_pre_post_topic(self, topic: str) -> None:
        """
        Called from post_topic before the topic is posted. For slack, this also sets the channel topic
        """
        self._bot.api_call(
            "channels.setTopic",
            {
                "channel": self.config["TOPIC_CHANNEL_ID"],
                "topic": topic,
            },
        )
