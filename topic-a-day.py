from datetime import datetime
from hashlib import md5
import random
from typing import Any
from typing import List
from typing import Dict
from threading import RLock

from errbot.backends.base import Message as ErrbotMessage
from errbot import BotPlugin
from errbot import Command
from errbot import ValidationException
from errbot import arg_botcmd
from errbot import botcmd
from decouple import config as get_config
import schedule
from wrapt import synchronized  # https://stackoverflow.com/a/29403915

TOPICS_LOCK = RLock()


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


class Topics(object):
    """
    Topics are our topics that we want to post. This is basically a big wrapper class around a python list of
    dicts that uses the Errbot Storage engine to store itself.

    In the future, I'd like to see this replaced by some sort of database engine to make this more robust. This
    is good enough for a MVP.
    """

    def __init__(self, bot_plugin: BotPlugin) -> None:
        self.bot_plugin = bot_plugin
        try:
            _ = self.bot_plugin["TOPICS"]
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
                "date_used": None,
            }
        )
        self.bot_plugin["TOPICS"] = topics
        return

    def get_random(self) -> Dict:
        """
        Returns a random, unused topic
        """
        return random.choice(
            list(filter(lambda d: not d["used"], self.bot_plugin["TOPICS"]))
        )

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
                self.bot_plugin["TOPICS"] = topics
                found = True
        if not found:
            raise KeyError("%s not found in topic list", topic_id)
        return

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
                break
        if not found:
            raise KeyError("%s not found in topic list", topic_id)
        topics.pop(index)
        self.bot_plugin["TOPICS"] = topics
        return

    @staticmethod
    def hash_topic(topic: str) -> str:
        """
        Returns an 8 character id hash of a topic with the current datetime (for uniqueness)
        """
        return md5(f"{topic}-{datetime.now()}".encode("utf-8")).hexdigest()[:8]

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
        for day in self.config["TOPIC_DAYS"]:
            getattr(schedule.every(), day).at(self.config["TOPIC_TIME"]).do(
                self.post_topic
            )

        self.start_poller(
            self.config["TOPIC_POLLER_INTERVAL"], self.run_scheduled_jobs, None
        )

    def deactivate(self) -> None:
        """
        Deactivates the plugin by stopping our scheduled jobs poller
        """
        # self.stop_poller(self.config['TOPIC_POLLER_INTERVAL'], self.run_scheduled_jobs)
        super().deactivate()

    def configure(self, configuration: Dict) -> None:
        """
        Configures the plugin
        """
        self.log.debug("Starting Config")
        if configuration is None:
            configuration = dict()

        # name of the channel to post in
        get_config_item("TOPIC_CHANNEL", configuration)
        configuration["TOPIC_CHANNEL_ID"] = self._bot.channelname_to_channelid(
            configuration["TOPIC_CHANNEL"]
        )
        # Days to post a topic. Comma separated list of day names
        get_config_item(
            "TOPIC_DAYS",
            configuration,
            cast=lambda v: [s.lower() for s in v.split(",")],
            default="monday,tuesday,wednesday,thursday,friday",
        )
        # what time the topic is posted every day, 24hr notation
        get_config_item("TOPIC_TIME", configuration, default="09:00")
        # How frequently the poller runs. Lower numbers might result in higher load
        get_config_item("TOPIC_POLLER_INTERVAL", configuration, default=5, cast=int)
        super().configure(configuration)

    def check_configuration(self, configuration: Dict) -> None:
        """
        Validates our config
        Raises:
            errbot.ValidationException when the configuration is invalid
        """
        if configuration["TOPIC_CHANNEL"][0] != "#":
            raise ValidationException(
                "TOPIC_CHANNEL should be in the format #channel-name"
            )

        VALID_DAY_NAMES = set(
            "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"
        )
        invalid_days = [
            day for day in configuration["TOPIC_DAYS"] if day not in VALID_DAY_NAMES
        ]
        if len(invalid_days) > 0:
            raise ValidationException("TOPIC_DAYS invalid %s", invalid_days)

        # TODO: Write more configuration validation
        return

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

    @botcmd
    def list_topics(self, msg: ErrbotMessage, args: List) -> None:
        """
        Lists all of our topics
        """
        topics = self.topics.list()
        used_topics = []
        free_topics = []
        for topic in topics:
            if topic["used"]:
                used_topics.append(
                    f"{topic['id']}: {topic['topic']} -- Posted on {topic['date_used']}"
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

    def post_topic(self) -> None:
        new_topic = self.topics.get_random()
        topic_template = f"Today's Topic: {new_topic['topic']}"
        self._bot.api_call(
            "channels.setTopic",
            {"channel": self.config["TOPIC_CHANNEL_ID"], "topic": topic_template},
        )
        self.send(self.build_identifier(self.config["TOPIC_CHANNEL"]), topic_template)
        self.topics.set_used(new_topic["id"])

    def run_scheduled_jobs(self) -> None:
        """
        Run by an errbot poller to run schedule jobs
        """
        schedule.run_pending()
