#!/usr/bin/env python
# Fedora Hosted Processor - Config
# Ricky Elrod <codeblock@fedoraproject.org>
# GPLv2+

# Set this False for production(!)
# It's important because if you don't, tracebacks will literally give users
# a full Python REPL.
DEBUG = True

# Make this random.
SECRET_KEY = 'MySecretKeyThatShouldBeChangedSoChangeItPleaseKTHX!!11111eleven'

# Self explainatory.
SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/test.db'

# This is only used to check of a group exists -- a readonly user works fine.
FAS_USERNAME = 'someuser'
FAS_PASSWORD = 'SomeP4ssw0rd!!!'
