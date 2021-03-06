#!/usr/bin/env python
# Fedora Hosted Processor
# (c) 2012 Red Hat, Inc.
# Ricky Elrod <codeblock@fedoraproject.org>
# GPLv2+

from flask import Flask, request, session, g, redirect, url_for, abort, \
    render_template, flash, jsonify
from flask.ext.sqlalchemy import SQLAlchemy
from sqlalchemy.orm import class_mapper
from sqlalchemy.orm.properties import RelationshipProperty
from flaskext.wtf import Form, BooleanField, TextField, SelectField, \
    TextAreaField, validators, FieldList, ValidationError
from flaskext.mail import Mail, Message
from datetime import datetime
import fedora.client

app = Flask(__name__)
mail = Mail()

app.config.from_envvar('FEDORAHOSTED_CONFIG')
db = SQLAlchemy(app)
mail.init_app(app)


class JSONifiable(object):
    """ A mixin for sqlalchemy models providing a .__json__ method. """

    def __json__(self, seen=None):
        """ Returns a dict representation of the object.

        Recursively evaluates .__json__() on its relationships.
        """

        if not seen:
            seen = []

        properties = list(class_mapper(type(self)).iterate_properties)
        relationships = [
            p.key for p in properties if type(p) is RelationshipProperty
        ]
        attrs = [
            p.key for p in properties if p.key not in relationships
        ]

        d = dict([(attr, getattr(self, attr)) for attr in attrs])

        for attr in relationships:
            d[attr] = self._expand(getattr(self, attr), seen)

        return d

    def _expand(self, relation, seen):
        """ Return the __json__() or id of a sqlalchemy relationship. """

        if hasattr(relation, 'all'):
            relation = relation.all()

        if hasattr(relation, '__iter__'):
            return [self._expand(item, seen) for item in relation]

        if type(relation) not in seen:
            return relation.__json__(seen + [type(self)])
        else:
            return relation.id


# TODO: Move these out to their own file.
class MailingList(db.Model, JSONifiable):
    id = db.Column(db.Integer, primary_key=True)
    # mailman does not enforce a hard limit. SMTP specifies 64-char limit
    # on local-part, so use that.
    name = db.Column(db.String, unique=True)

    @classmethod
    def find_or_create_by_name(self, name):
        """
        If a list with the given name exists, return it.
        Otherwise create it, *then* return it.
        """
        lists = self.query.filter_by(name=name)
        if lists.count() > 0:
            return lists.first()
        else:
            new_list = self(name=name)
            db.session.add(new_list)
            db.session.commit()
            return new_list


class ListRequest(db.Model, JSONifiable):
    id = db.Column(db.Integer, primary_key=True)
    commit_list = db.Column(db.Boolean, default=False)

    mailing_list_id = db.Column(db.Integer,
                                db.ForeignKey('mailing_list.id'))
    mailing_list = db.relationship('MailingList',
                                   backref=db.backref(
                                       'list_request', lazy='dynamic'))

    hosted_request_id = db.Column(db.Integer,
                                  db.ForeignKey('hosted_request.id'))
    hosted_request = db.relationship('HostedRequest',
                                     backref=db.backref(
                                         'list_request', lazy='dynamic'))


class HostedRequest(db.Model, JSONifiable):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True)
    pretty_name = db.Column(db.String(150), unique=True)
    description = db.Column(db.String(255))
    scm = db.Column(db.String(10))
    trac = db.Column(db.String(5))
    owner = db.Column(db.String(32))  # 32 is the max username length in FAS
    created = db.Column(db.DateTime, default=datetime.now())
    completed = db.Column(db.DateTime, default=None)
    mailing_lists = db.relationship('MailingList',
                                    secondary=ListRequest.__table__,
                                    backref=db.backref('hosted_requests',
                                                       lazy='dynamic'))
    comments = db.Column(db.String)


def valid_mailing_list_name(form, mailing_list):
    if not mailing_list.data:
        return
    if not mailing_list.data.startswith(form.project_name.data + '-'):
        raise ValidationError("Mailing lists must start with the project "
                              "name and a dash, e.g. '%s-users'" % (
                              form.project_name.data))


def valid_email_address(form, commit_list):
    if not commit_list.data:
        return
    if not '@' in commit_list.data:
        raise ValidationError("Commit list must be a full email-address "
                              "(e.g. contain a '@' sign).")


class RequestForm(Form):
    project_name = TextField('Name (lowercase, alphanumeric only)',
                             [validators.Length(min=1, max=150)])
    project_pretty_name = TextField('Pretty Name',
                                    [validators.Length(min=1, max=150)])
    project_description = TextField('Short Description',
                                    [validators.Length(min=1, max=254)])
    project_owner = TextField('Owner FAS Username',
                              [validators.Length(min=1, max=32)])
    project_scm = SelectField('SCM',
                              choices=[('git', 'git'),
                                       ('svn', 'svn'),
                                       ('bzr', 'bzr'),
                                       ('hg', 'hg')])
    project_trac = SelectField('Trac Instance?',
                               choices=[('no', 'No'),
                                        ('yes', 'Yes'),
                                        ('agilo', 'Yes w/ Agilo')])
    project_mailing_lists = FieldList(
        TextField('Mailing List (must start with the project name)',
                  [validators.Length(max=64), valid_mailing_list_name]),
        min_entries=1)
    project_commit_lists = FieldList(
        TextField('Send commit emails to (full email address)',
                  [valid_email_address]),
        min_entries=1)
    comments = TextAreaField('Comments/Special Requests')


def scm_push_instructions(project):
    # TODO: Fix bzr branch name (???)
    if project.scm == 'git':
        return "git push ssh://git.fedorahosted.org/git/%s.git/ master" % (
            project.name)
    elif project.scm == 'bzr':
        return "bzr branch bzr://bzr.fedorahosted.org/bzr/%s/[branch]" % (
            project.name)
    elif project.scm == 'svn':
        return "svn co svn+ssh://svn.fedorahosted.org/svn/%s" % project.name


@app.route('/', methods=['POST', 'GET'])
def hello():
    form = RequestForm()
    if form.validate_on_submit():
        # The hosted request itself (no mailing lists)
        hosted_request = HostedRequest(
            name=form.project_name.data,
            pretty_name=form.project_pretty_name.data,
            description=form.project_description.data,
            scm=form.project_scm.data,
            trac=form.project_trac.data,
            owner=form.project_owner.data,
            comments=form.comments.data)
        db.session.add(hosted_request)
        db.session.commit()

        # Mailing lists
        for entry in form.project_mailing_lists.entries:
            if entry.data:
                # The field wasn't left blank...
                list_name = entry.data

                # The person only entered the list name, not the full address.
                if not list_name.endswith("@lists.fedorahosted.org"):
                    list_name = list_name + "@lists.fedorahosted.org"

                mailing_list = MailingList.find_or_create_by_name(list_name)

                # The last step before storing the list is handling
                # commit lists -- lists that get commit messages -- which also
                # appear as regular lists.
                commit_list = False
                if list_name in form.project_commit_lists.entries:
                    del form.project_commit_lists.entries[list_name]
                    commit_list = True

                # Now we have a mailing list object (in mailing_list), we can
                # store the relationship using it.
                list_request = ListRequest(
                    mailing_list=mailing_list,
                    hosted_request=hosted_request,
                    commit_list=commit_list)
                db.session.add(list_request)
                db.session.commit()

        # Add the remaining commit list entries to the project.
        for entry in form.project_commit_lists.entries:
            if entry.data:
                mailing_list = MailingList.find_or_create_by_name(entry.data)
                list_request = ListRequest(
                    mailing_list=mailing_list,
                    hosted_request=hosted_request,
                    commit_list=True)
                db.session.add(list_request)
                db.session.commit()

        # Tell some people that the request has been made.
        message = Message("New Fedora Hosted project request")
        message.body = """Members of sysadmin-hosted,

A new Fedora Hosted request, id %s,  has been made.
To process this request, please do the following:

$ ssh fedorahosted.org
$ fedorahosted -n -p %s    # No-op. Review this and make sure the output looks
                          # sane.

$ sudo fedorahosted -p %s  # Actually process the request.

Thanks,
Fedora Hosted automation system""" % (hosted_request.id,
                                      hosted_request.id,
                                      hosted_request.id)
        message.recipients = [app.config['NOTIFY_ON_REQUEST']]
        message.sender = \
            "Fedora Hosted <sysadmin-hosted-members@fedoraproject.org>"
        if not app.config['TESTING']:
            mail.send(message)

        return render_template('completed.html')

    # GET, not POST.
    return render_template('index.html', form=form)


@app.route('/pending')
def pending():
    requests = HostedRequest.query.filter_by(completed=None)
    return render_template('pending.html', requests=requests)


@app.route('/getrequest')
def get_request():
    """Returns a JSON representation of a Fedora Hosted Request."""
    hosted_request = HostedRequest.query.filter_by(id=request.args.get('id'))
    if hosted_request.count() > 0:
        request_json = hosted_request.first().__json__()
        del request_json['mailing_lists']

        # Flask 0.8 can't handle JSONifying DateTime objects, so we convert it
        # to a String instead.
        request_json['created'] = request_json['created'].isoformat()
        if request_json['completed']:
            request_json['completed'] = request_json['completed'].isoformat()

        return jsonify(request_json)
    else:
        return jsonify(error="No hosted request with that ID could be found.")


@app.route('/mark-completed')
def mark_complete():
    """
    Checks to see if a group exists in FAS for the given project and marks the
    project complete if it does. We do this this way so that we don't have to
    send FAS credentials to this app.
    """
    fas = fedora.client.AccountSystem(app.config['FAS_SERVER'],
                                      username=app.config['FAS_USERNAME'],
                                      password=app.config['FAS_PASSWORD'],
                                      insecure=app.config['FAS_INSECURE_SSL'])
    hosted_request = HostedRequest.query.filter_by(id=request.args.get('id'))
    if hosted_request.count() > 0:
        project = hosted_request[0]
        if project.completed:
            return jsonify(error="Request was already marked as completed.")

        group_name = project.scm + project.name
        try:
            group = fas.group_by_name(group_name)
        except:
            return jsonify(error="No such group: " + group_name)

        project.completed = datetime.now()
        db.session.commit()
        message = Message("Your Fedora Hosted request has been processed")
        message.body = """Hi there,

You're receiving this message because the Fedora Hosted project:
  %s
has been set up.

To access to your new repository, do the following:
  $ %s

If you've requested a Trac instance, you can visit it at:
  https://fedorahosted.org/%s

If you've requested any mailing lists, you should have received separate
emails which contain instructions on how to administrate them.

Sincerely,
Fedora Hosted""" % (
            project.name,
            scm_push_instructions(project),
            project.name)

        message.sender = \
            "Fedora Hosted <sysadmin-hosted-members@fedoraproject.org>"

        if 'PROJECT_OWNER_EMAIL_OVERRIDE' in app.config:
            message.recipients = [app.config['PROJECT_OWNER_EMAIL_OVERRIDE']]
        else:
            message.recipients = ["%s@fedoraproject.org" % project.owner]

        if not app.config['TESTING']:
            mail.send(message)

        return jsonify(success="Request marked as completed.")
    else:
        return jsonify(error="No hosted request with that ID could be found.")

if __name__ == "__main__":
    app.run(host='0.0.0.0')
