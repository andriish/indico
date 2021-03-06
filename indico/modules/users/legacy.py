# This file is part of Indico.
# Copyright (C) 2002 - 2020 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from flask_multipass import IdentityInfo
from zope.interface.declarations import implementer

from indico.legacy.common.cache import GenericCache
from indico.legacy.fossils.user import IAvatarFossil, IAvatarMinimalFossil
from indico.modules.auth import Identity
from indico.modules.users import User, logger
from indico.util.caching import memoize_request
from indico.util.fossilize import Fossilizable
from indico.util.locators import locator_property


AVATAR_FIELD_MAP = {
    'email': 'email',
    'name': 'first_name',
    'surName': 'last_name',
    'organisation': 'affiliation'
}


@implementer(IAvatarFossil, IAvatarMinimalFossil)
class AvatarUserWrapper(Fossilizable):
    """Avatar-like wrapper class that holds a DB-stored user."""

    def __init__(self, user_id):
        self.id = str(user_id)

    @property
    @memoize_request
    def _original_user(self):
        # A proper user, with an id that can be mapped directly to sqlalchemy
        if isinstance(self.id, int) or self.id.isdigit():
            return User.get(int(self.id))
        # A user who had no real indico account but an ldap identifier/email.
        # In this case we try to find his real user and replace the ID of this object
        # with that user's ID.
        data = self.id.split(':')
        # TODO: Once everything is in SQLAlchemy this whole thing needs to go away!
        user = None
        if data[0] == 'LDAP':
            identifier = data[1]
            email = data[2]
            # You better have only one ldap provider or at least different identifiers ;)
            identity = Identity.query.filter(Identity.provider != 'indico', Identity.identifier == identifier).first()
            if identity:
                user = identity.user
        elif data[0] == 'Nice':
            email = data[1]
        else:
            return None
        if not user:
            user = User.query.filter(User.all_emails == email).first()
        if user:
            self._old_id = self.id
            self.id = str(user.id)
            logger.info("Updated legacy user id (%s => %s)", self._old_id, self.id)
        return user

    @property
    @memoize_request
    def user(self):
        user = self._original_user
        if user is not None and user.is_deleted and user.merged_into_id is not None:
            while user.merged_into_id is not None:
                user = user.merged_into_user
        return user

    def getId(self):
        return str(self.user.id) if self.user else str(self.id)

    @property
    def api_key(self):
        return self.user.api_key if self.user else None

    def getStatus(self):
        return 'deleted' if not self.user or self.user.is_deleted else 'activated'

    def isActivated(self):
        # All accounts are activated during the transition period
        return True

    def isDisabled(self):
        # The user has been blocked or deleted (due to merge)
        return not self.user or self.user.is_blocked or self.user.is_deleted

    def getName(self):
        return self.user.first_name if self.user else ''

    getFirstName = getName

    def getSurName(self):
        return self.user.last_name if self.user else ''

    getFamilyName = getSurName

    def getFullName(self):
        if not self.user:
            return ''
        return self.user.get_full_name(last_name_first=True, last_name_upper=True,
                                       abbrev_first_name=False, show_title=False)

    def getStraightFullName(self, upper=True):
        if not self.user:
            return ''
        return self.user.get_full_name(last_name_first=False, last_name_upper=upper,
                                       abbrev_first_name=False, show_title=False)

    getDirectFullNameNoTitle = getStraightFullName

    def getAbrName(self):
        if not self.user:
            return ''
        return self.user.get_full_name(last_name_first=True, last_name_upper=False,
                                       abbrev_first_name=True, show_title=False)

    def getStraightAbrName(self):
        if not self.user:
            return ''
        return self.user.get_full_name(last_name_first=False, last_name_upper=False,
                                       abbrev_first_name=True, show_title=False)

    def getOrganisation(self):
        return self.user.affiliation if self.user else ''

    getAffiliation = getOrganisation

    def getTitle(self):
        return self.user.title if self.user else ''

    def getAddress(self):
        return self.user.address if self.user else ''

    def getEmails(self):
        # avoid 'stale association proxy'
        user = self.user
        return set(user.all_emails) if user else set()

    def getEmail(self):
        return self.user.email if self.user else ''

    email = property(getEmail)

    def hasEmail(self, email):
        user = self.user  # avoid 'stale association proxy'
        if not user:
            return False
        return email.lower() in user.all_emails

    def getTelephone(self):
        return self.user.phone if self.user else ''

    def getFax(self):
        # Some older code still clones fax, etc...
        # it's never shown in the interface anyway.
        return ''

    getPhone = getTelephone

    def canUserModify(self, avatar):
        if not self.user:
            return False
        return avatar.id == str(self.user.id) or avatar.user.is_admin

    @locator_property
    def locator(self):
        d = {}
        if self.user:
            d['userId'] = self.user.id
        return d

    def isAdmin(self):
        if not self.user:
            return False
        return self.user.is_admin

    @property
    def as_new(self):
        return self.user

    def __eq__(self, other):
        if not isinstance(other, (AvatarUserWrapper, User)):
            return False
        elif str(self.id) == str(other.id):
            return True
        elif self.user:
            return str(self.user.id) == str(other.id)
        else:
            return False

    def __ne__(self, other):
        return not (self == other)

    def __hash__(self):
        return hash(str(self.id))

    def __repr__(self):
        if self.user is None:
            return f'<AvatarUserWrapper {self.id}: user does not exist>'
        elif self._original_user.merged_into_user:
            return '<AvatarUserWrapper {}: {} ({}) [{}]>'.format(
                self.id, self._original_user.full_name, self._original_user.email, self.user.id)
        else:
            return f'<AvatarUserWrapper {self.id}: {self.user.full_name} ({self.user.email})>'


@implementer(IAvatarFossil, IAvatarMinimalFossil)
class AvatarProvisionalWrapper(Fossilizable):
    """Wrap provisional data for users that are not in the DB yet."""

    def __init__(self, identity_info):
        self.identity_info = identity_info
        self.data = identity_info.data

    def getId(self):
        return f"{self.identity_info.provider.name}:{self.identity_info.identifier}"

    id = property(getId)

    def getEmail(self):
        return self.data['email']

    def getEmails(self):
        return [self.data['email']]

    def getFirstName(self):
        return self.data.get('first_name', '')

    def getFamilyName(self):
        return self.data.get('last_name', '')

    def getStraightFullName(self, upper=False):
        last_name = self.data.get('last_name', '')
        if upper:
            last_name = last_name.upper()
        return '{} {}'.format(self.data.get('first_name', ''), last_name)

    def getTitle(self):
        return ''

    def getTelephone(self):
        return self.data.get('phone', '')

    getPhone = getTelephone

    def getOrganisation(self):
        return self.data.get('affiliation', '')

    getAffiliation = getOrganisation

    def getFax(self):
        return None

    def getAddress(self):
        return ''

    def __repr__(self):
        return '<AvatarProvisionalWrapper {}: {} ({first_name} {last_name})>'.format(
            self.identity_info.provider.name,
            self.identity_info.identifier,
            **self.data.to_dict())


def search_avatars(criteria, exact=False, search_externals=False):
    from indico.modules.users.util import search_users

    if not any(criteria.values()):
        return []

    def _process_identities(obj):
        if isinstance(obj, IdentityInfo):
            GenericCache('pending_identities').set(f'{obj.provider.name}:{obj.identifier}', obj.data)
            return AvatarProvisionalWrapper(obj)
        else:
            return obj.as_avatar

    results = search_users(exact=exact, external=search_externals,
                           **{AVATAR_FIELD_MAP[k]: v for (k, v) in criteria.items() if v})

    return [_process_identities(obj) for obj in results]
