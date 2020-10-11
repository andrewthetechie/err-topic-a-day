# err-topic-a-day
An errbot plugin to post a topic a day to a channel for discussion

# Configuration
topic-a-day reads its config from a combination of environment variables and config files. This is due to the complexity
of expressing some config options as env variable strings.

Config options:

* TAD_CHANNEL: str, channel name to use as your topic channel
* TAD_SCHEDULE: str, schedule to post on, expressed as a crontab. This uses apscheduler, which starts its cron day counts
on monday, rather than sunday
* TAD_APSCHEDULER_CONFIG_FILE: str, optional. Path to a config file for 
[AP Scheduler's config](https://apscheduler.readthedocs.io/en/stable/userguide.html#configuring-the-scheduler). If left
blank, we setup a basic, working config
* TAD_TZ: What timezone to set. Only used if TAD_APSCHEDULER_CONFIG_FILE is false.
* TAD_ENABLE_WEBHOOK: bool, default false. If enabled, use a webhook + curl to perform posting. Allows using the AP
scheduler sqlalchemy backend
* TAD_WEBHOOK_URL, str, url for the Topic a day webhook. Defaults to localhost:3142/post_topic_rpc
* AUTH_POST_WEBHOOK: bool, default True. If true, webhook only works if auth'd properly
* AUTH_POST_WEBHOOK_TOKEN: str, default generated token. The token for our webhook auth
