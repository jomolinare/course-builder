# coding: utf-8
# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module to support internationalization (i18n) workflow."""

__author__ = 'John Orr (jorr@google.com)'

import cgi
import collections
import cStringIO
import datetime
import logging
import os
import re
import StringIO
import sys
import urllib
from xml.dom import minidom
import zipfile

from babel import localedata
from babel.messages import catalog
from babel.messages import pofile
import jinja2
from webapp2_extras import i18n

import appengine_config
from common import caching
from common import crypto
from common import locales as common_locales
from common import resource
from common import safe_dom
from common import schema_fields
from common import tags
from common import utils as common_utils
from common import xcontent
from controllers import sites
from controllers import utils
from models import courses
from models import resources_display
from models import custom_modules
from models import custom_units
from models import jobs
from models import models
from models import roles
from models import transforms
from models.config import ConfigProperty
from models.counters import PerfCounter
from modules.dashboard import dashboard
from modules.dashboard import unit_lesson_editor
from modules.oeditor import oeditor
from tools import verify

from google.appengine.ext import db

RESOURCES_PATH = '/modules/i18n_dashboard/resources'

TEMPLATES_DIR = os.path.join(
    appengine_config.BUNDLE_ROOT, 'modules', 'i18n_dashboard', 'templates')

# The path to the CSS file with application-wide i18n-related styling
GLOBAL_CSS = '/modules/i18n_dashboard/resources/css/global_i18n.css'

VERB_NEW = xcontent.SourceToTargetDiffMapping.VERB_NEW
VERB_CHANGED = xcontent.SourceToTargetDiffMapping.VERB_CHANGED
VERB_CURRENT = xcontent.SourceToTargetDiffMapping.VERB_CURRENT

# This permission grants the user access to the i18n dashboard and console.
ACCESS_PERMISSION = 'access_i18n_dashboard'
ACCESS_PERMISSION_DESCRIPTION = 'Can access I18n Dashboard.'

TYPE_HTML = 'html'
TYPE_STRING = 'string'
TYPE_TEXT = 'text'
TYPE_URL = 'url'

# Filter for those schema fields which are translatable
TRANSLATABLE_FIELDS_FILTER = schema_fields.FieldFilter(
    type_names=[TYPE_HTML, TYPE_STRING, TYPE_TEXT, TYPE_URL],
    hidden_values=[False],
    i18n_values=[None, True],
    editable_values=[True])

# Here, using 'ln' because we need a language that Babel knows.
# Lingala ( http://en.wikipedia.org/wiki/Lingala ) is not likely to be
# a target language for courses hosted in CB in the next few years.
PSEUDO_LANGUAGE = 'ln'

custom_module = None


class ResourceBundleKey(object):
    """Manages a key for a resource bundle."""

    def __init__(self, type_str, key, locale):
        self._locale = locale
        self._type = type_str
        self._key = key

    def __str__(self):
        return '%s:%s:%s' % (self._type, self._key, self._locale)

    @property
    def locale(self):
        return self._locale

    @property
    def resource_key(self):
        return resource.Key(self._type, self._key)

    @classmethod
    def fromstring(cls, key_str):
        type_str, key, locale = key_str.split(':', 2)
        return ResourceBundleKey(type_str, key, locale)

    @classmethod
    def from_resource_key(cls, resource_key, locale):
        return cls(resource_key.type, resource_key.key, locale)


class NamedJsonDAO(models.BaseJsonDao):
    """Base class for DAOs of entities with named keys."""

    ENTITY_KEY_TYPE = models.BaseJsonDao.EntityKeyTypeName

    @classmethod
    def load_or_default(cls, resource_key):
        dto = cls.load(str(resource_key))
        if not dto:
            dto = cls.create_blank(resource_key)
        return dto

    @classmethod
    def create_blank(cls, resource_key):
        return cls.DTO(str(resource_key), {})


class I18nProgressEntity(models.BaseEntity):
    """The base entity for storing i18n workflow information.

    Each entity represents one resource in the course.
    """

    data = db.TextProperty(indexed=False)


class I18nProgressDTO(object):
    """The lightweight data object for the i18n workflow data."""

    NOT_STARTED = 0
    IN_PROGRESS = 1
    DONE = 2

    IS_I18N_KEY = 'is_i18n'
    PROGRESS_KEY = 'progress'

    def __init__(self, the_id, the_dict):
        self.id = the_id
        self.dict = the_dict

    @property
    def is_translatable(self):
        return self.dict.get(self.IS_I18N_KEY, True)

    @is_translatable.setter
    def is_translatable(self, value):
        assert type(value) == bool
        self.dict[self.IS_I18N_KEY] = value

    def get_progress(self, locale):
        return self.dict.get(self.PROGRESS_KEY, {}).get(
            locale, self.NOT_STARTED)

    def set_progress(self, locale, value):
        progress_dict = self.dict.setdefault(self.PROGRESS_KEY, {})
        progress_dict[locale] = value

    def clear_progress(self, locale):
        self.dict.get(self.PROGRESS_KEY, {}).pop(locale, None)


class I18nProgressDAO(NamedJsonDAO):
    """Access object for the i18n workflow data."""

    DTO = I18nProgressDTO
    ENTITY = I18nProgressEntity


class ResourceBundleEntity(models.BaseEntity):
    """The base entity for storing i18n resource bundles."""

    data = db.TextProperty(indexed=False)
    locale = db.StringProperty(indexed=True)
    created_on = db.DateTimeProperty(auto_now_add=True, indexed=False)
    updated_on = db.DateTimeProperty(indexed=True)

    @classmethod
    def getsizeof(cls, entity):
        return (
            sys.getsizeof(entity.data) +
            sys.getsizeof(entity.locale) +
            sys.getsizeof(entity.created_on) +
            sys.getsizeof(entity.updated_on))


class ResourceBundleDTO(object):
    """The lightweight data transfer object for resource bundles.

    Resource bundles are keyed by (resource_type, resource_key, locale). The
    data stored in the dict follows the following pattern:

    {
      field_name_1: {
        type: <the value type for the field>,
        source_value: <only used for html type: the undecomposed source_value>,
        data: [
          # A list of source/target pairs. The list is a singleton for plain
          # string data, and is a list of decomposed chunks for html data
          {
            source_value: <the original untranslated string>,
            target_value: <the translated string>,
          }
        ]
      },
      field_name_2: ...
    }
    """

    def __init__(self, the_id, the_dict):
        self.id = the_id
        self.dict = the_dict


class ResourceBundleDAO(NamedJsonDAO):
    """Data access object for resource bundle information."""

    DTO = ResourceBundleDTO
    ENTITY = ResourceBundleEntity

    @classmethod
    def before_put(cls, dto, entity):
        resource_bundle_key = ResourceBundleKey.fromstring(dto.id)
        entity.locale = resource_bundle_key.locale
        entity.updated_on = datetime.datetime.utcnow()

    @classmethod
    def get_all_for_locale(cls, locale):
        query = caching.iter_all(
            cls.ENTITY.all().filter('locale = ', locale))
        return [
            cls.DTO(entity.key().id_or_name(), transforms.loads(entity.data))
            for entity in query]

    @classmethod
    def delete_all_for_locale(cls, locale):
        # It would be nice if AppEngine DB had a query formulation that
        # allowed for deletion, but apparently not so much.  Here, at least
        # we are only round-tripping the keys, not the whole objects through
        # memory.
        db.delete(list(common_utils.iter_all(
            cls.ENTITY.all(keys_only=True).filter('locale = ', locale))))


class TableRow(object):
    """Class to represent a row in the dashboard table."""

    @property
    def name(self):
        raise NotImplementedError()

    @property
    def class_name(self):
        return ''

    @property
    def spans_all_columns(self):
        return False


def _build_resource_title(app_context, rsrc_type, rsrc):
    if rsrc_type == resources_display.ResourceUnit.TYPE:
        title = resources_display.display_unit_title(rsrc, app_context)
    elif rsrc_type == resources_display.ResourceLesson.TYPE:
        title = resources_display.display_lesson_title(
            rsrc[0], rsrc[1], app_context)
    else:
        resource_handler = resource.Registry.get(rsrc_type)
        title = resource_handler.get_resource_title(rsrc)
    return title


class ResourceRow(TableRow):
    """A row in the dashboard table which displays status of a CB resource."""

    DONE_CLASS = 'done'
    DONE_STRING = 'Done'
    IN_PROGRESS_CLASS = 'in-progress'
    IN_PROGRESS_STRING = 'In progress'
    NOT_STARTED_CLASS = 'not-started'
    NOT_STARTED_STRING = 'Not started'
    NOT_TRANSLATABLE_CLASS = 'not-translatable'

    def __init__(
            self, course, rsrc, type_str, key,
            i18n_progress_dto=None, resource_key=None):
        self._course = course
        self._resource = rsrc
        self._type = type_str
        self._key = key
        if i18n_progress_dto is None:
            assert resource_key
            self._i18n_progress_dto = I18nProgressDAO.create_blank(resource_key)
        else:
            self._i18n_progress_dto = i18n_progress_dto

    @property
    def name(self):
        return _build_resource_title(
            self._course.app_context, self._type, self._resource)

    @property
    def class_name(self):
        if self._i18n_progress_dto.is_translatable:
            return ''
        else:
            return self.NOT_TRANSLATABLE_CLASS

    @property
    def resource_key(self):
        return resource.Key(self._type, self._key, course=self._course)

    @property
    def is_translatable(self):
        return self._i18n_progress_dto.is_translatable

    def status(self, locale):
        progress = self._i18n_progress_dto.get_progress(locale)
        if progress == I18nProgressDTO.NOT_STARTED:
            return self.NOT_STARTED_STRING
        elif progress == I18nProgressDTO.IN_PROGRESS:
            return self.IN_PROGRESS_STRING
        else:
            return self.DONE_STRING

    def status_class(self, locale):
        progress = self._i18n_progress_dto.get_progress(locale)
        if progress == I18nProgressDTO.NOT_STARTED:
            return self.NOT_STARTED_CLASS
        elif progress == I18nProgressDTO.IN_PROGRESS:
            return self.IN_PROGRESS_CLASS
        else:
            return self.DONE_CLASS

    def view_url(self, locale):
        resource_handler = resource.Registry.get(self._type)
        view_url = resource_handler.get_view_url(self._resource)
        if view_url:
            view_url += '&hl=%s' % locale
        return view_url

    def edit_url(self, locale):
        return TranslationConsole.get_edit_url(
            ResourceBundleKey(self._type, self._key, locale))

    @property
    def base_view_url(self):
        return self.view_url(None)

    @property
    def base_edit_url(self):
        return resource.Registry.get(self._type).get_edit_url(self._key)


class SectionRow(TableRow):
    """A row in the table which serves as a section heading."""

    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def class_name(self):
        return 'section-row'

    @property
    def spans_all_columns(self):
        return True


class EmptyRow(SectionRow):
    """A multi-column row in the table which indicates an empty section."""

    def __init__(self, name='Empty section', class_name='empty_section'):
        super(EmptyRow, self).__init__(name)
        self._class_name = class_name

    @property
    def class_name(self):
        return self._class_name


class IsTranslatableRestHandler(utils.BaseRESTHandler):
    """REST handler to respond to setting a resource as (non-)translatable."""

    URL = '/rest/modules/i18n_dashboard/is_translatable'
    XSRF_TOKEN_NAME = 'is-translatable'

    def post(self):
        request = transforms.loads(self.request.get('request'))
        if not self.assert_xsrf_token_or_fail(
                request, self.XSRF_TOKEN_NAME, {}):
            return

        if not unit_lesson_editor.CourseOutlineRights.can_edit(self):
            transforms.send_json_response(self, 401, 'Access denied.', {})
            return

        payload = request.get('payload')
        i18n_progress_dto = I18nProgressDAO.load_or_default(
            payload['resource_key'])
        i18n_progress_dto.is_translatable = payload['value']
        I18nProgressDAO.save(i18n_progress_dto)

        transforms.send_json_response(self, 200, 'OK', {}, None)


class BaseDashboardExtension(object):
    ACTION = None

    @classmethod
    def is_readonly(cls, course):
        return course.app_context.get_environ()[
                'course'].get('prevent_translation_edits')

    @classmethod
    def format_readonly_message(cls):
        return safe_dom.Element('P').add_text(
            'Translation console is currently disabled. '
            'Course administrator can enable it via I18N Settings.')

    @classmethod
    def register(cls):
        def get_action(handler):
            cls(handler).render()
        dashboard.DashboardHandler.add_custom_get_action(cls.ACTION, get_action)
        dashboard.DashboardHandler.map_action_to_permission(
            'get_%s' % cls.ACTION, ACCESS_PERMISSION)

    @classmethod
    def unregister(cls):
        dashboard.DashboardHandler.remove_custom_get_action(cls.ACTION)
        dashboard.DashboardHandler.unmap_action_to_permission(
            'get_%s' % cls.ACTION)

    def __init__(self, handler):
        """Initialize the class with a request handler.

        Args:
            handler: modules.dashboard.DashboardHandler. This is the handler
                which will do the rendering.
        """
        self.handler = handler


class TranslationsAndLocations(object):

    def __init__(self):
        self._translations = set()
        self._locations = []
        self._comments = []
        self._previous_id = ''

    def add_translation(self, translation):
        # Don't add "translations" that are blank, unless we have no other
        # alternatives.
        if translation or not self._translations:
            self._translations.add(translation)
        # If all we have so far is blank translations, and this one is
        # nonblank, throw away all the blank ones.
        if translation and not any(self._translations):
            self._translations = [translation]

    def add_location(self, location):
        self._locations.append(location)

    def add_comment(self, comment):
        comment = unicode(comment)  # May be Node or NodeList.
        self._comments.append(comment)

    def set_previous_id(self, previous_id):
        self._previous_id = previous_id

    @property
    def locations(self):
        return self._locations

    @property
    def translations(self):
        return self._translations

    @property
    def comments(self):
        return self._comments

    @property
    def previous_id(self):
        return self._previous_id


class I18nDeletionHandler(BaseDashboardExtension):
    ACTION = 'i18n_delete'

    def render(self):
        main_content = oeditor.ObjectEditor.get_html_for(
            self.handler,
            TranslationDeletionRestHandler.schema().get_json_schema(),
            TranslationDeletionRestHandler.schema().get_schema_dict(),
            '',
            self.handler.canonicalize_url(TranslationDeletionRestHandler.URL),
            self.handler.get_action_url(I18nDashboardHandler.ACTION),
            save_button_caption='Delete', auto_return=True,
            required_modules=TranslationDeletionRestHandler.REQUIRED_MODULES,
            extra_js_files=['delete_translations.js'],
            additional_dirs=[TEMPLATES_DIR])
        self.handler.render_page({
            'page_title': self.handler.format_title(
                'I18n Translation Deletion'),
            'main_content': main_content,
            }, in_action=I18nDashboardHandler.ACTION)


class TranslationDeletionRestHandler(utils.BaseRESTHandler):

    URL = '/rest/modules/i18n_dashboard/i18n_deletion'
    XSRF_TOKEN_NAME = 'translation_deletion'
    REQUIRED_MODULES = [
        'inputex-string', 'inputex-select', 'inputex-hidden',
        'inputex-checkbox', 'inputex-list', 'gcb-uneditable',
        ]

    @classmethod
    def schema(cls):
        schema = schema_fields.FieldRegistry('Translation Deletion')
        locales_schema = schema_fields.FieldRegistry(
            None, description='locales')
        locales_schema.add_property(schema_fields.SchemaField(
            'locale', 'Locale', 'string', hidden=True, editable=False))
        locales_schema.add_property(schema_fields.SchemaField(
            'checked', None, 'boolean'))
        locales_schema.add_property(schema_fields.SchemaField(
            'title', None, 'string', optional=True, editable=False))

        schema.add_property(schema_fields.FieldArray(
            'locales', 'Languages', item_type=locales_schema,
            description='Select the languages whose translations you '
            'wish to delete.',
            extra_schema_dict_values={
                'className': (
                    'inputEx-Field inputEx-ListField '
                    'label-group label-group-list')}))
        return schema

    def get(self):
        course = self.get_course()
        default_locale = course.default_locale
        locales = []
        for locale in course.all_locales:
            if locale == default_locale:
                continue
            locales.append({
                'locale': locale,
                'checked': False,
                'title': common_locales.get_locale_display_name(locale)})
        payload_dict = {
            'locales': locales,
            }
        transforms.send_json_response(
            self, 200, 'Success.', payload_dict=payload_dict,
            xsrf_token=crypto.XsrfTokenManager.create_xsrf_token(
                self.XSRF_TOKEN_NAME))

    def _validate_inputs(self, course):
        if appengine_config.PRODUCTION_MODE:
            transforms.send_json_response(
                self, 403, 'Not available in production.')
            return []

        try:
            request = models.transforms.loads(self.request.get('request'))
        except ValueError:
            transforms.send_json_response(
                self, 400, 'Malformed or missing "request" parameter.')
            return []
        try:
            payload = models.transforms.loads(request.get('payload', ''))
        except ValueError:
            transforms.send_json_response(
                self, 400, 'Malformed or missing "payload" parameter.')
            return []
        if not self.assert_xsrf_token_or_fail(
            request, self.XSRF_TOKEN_NAME, {}):
            return []

        try:
            locales = [l['locale'] for l in payload.get('locales')
                       if l.get('checked')]
        except (TypeError, ValueError, KeyError):
            transforms.send_json_response(
                self, 400, 'Locales specification not as expected.')
            return []
        if not locales:
            # Nice UI message when no locales selected.
            transforms.send_json_response(
                self, 400, 'Please select at least one language to delete.')
            return []
        for locale in locales:
            if not has_locale_rights(self.app_context, locale):
                transforms.send_json_response(self, 401, 'Access denied.')
                return []
        return locales

    @staticmethod
    def delete_locales(course, locales):
        # First remove progress indications.  If this fails or times out,
        # we haven't really lost any work; these can be rebuilt.
        i18n_progress_dtos = I18nProgressDAO.get_all()
        for i18n_progress_dto in i18n_progress_dtos:
            for locale in locales:
                i18n_progress_dto.clear_progress(locale)
        I18nProgressDAO.save_all(i18n_progress_dtos)

        # Now remove actual translations.
        for locale in locales:
            ResourceBundleDAO.delete_all_for_locale(locale)

        # When all of the foregoing has completed, remove the course
        # setting.  (Removing this earlier would be bad; removing this
        # tells the UI the locale is gone.  If we removed this first,
        # and then failed to remove locale items from the DB, confusion
        # would likely ensue)
        environ = course.get_environ(course.app_context)
        extra_locales = environ.get('extra_locales', [])
        for configured_locale in list(extra_locales):
            if configured_locale['locale'] in locales:
                extra_locales.remove(configured_locale)
        course.save_settings(environ)

    def put(self):
        """Verify inputs and return 200 OK to OEditor when all is well."""
        course = self.get_course()
        locales = self._validate_inputs(course)
        if not locales:
            return
        self.delete_locales(course, locales)
        transforms.send_json_response(self, 200, 'Success.')


class I18nDownloadHandler(BaseDashboardExtension):
    ACTION = 'i18n_download'

    def render(self):
        main_content = oeditor.ObjectEditor.get_html_for(
            self.handler,
            TranslationDownloadRestHandler.schema().get_json_schema(),
            TranslationDownloadRestHandler.schema().get_schema_dict(),
            '',
            self.handler.canonicalize_url(TranslationDownloadRestHandler.URL),
            self.handler.get_action_url(I18nDashboardHandler.ACTION),
            required_modules=TranslationDownloadRestHandler.REQUIRED_MODULES,
            save_button_caption='Download',
            extra_js_files=['download_translations.js'],
            additional_dirs=[TEMPLATES_DIR])
        self.handler.render_page({
            'page_title': self.handler.format_title(
                'I18n Translation Download'),
            'main_content': main_content,
            }, in_action=I18nDashboardHandler.ACTION)


class TranslationDownloadRestHandler(utils.BaseRESTHandler):

    URL = '/rest/modules/i18n_dashboard/i18n_download'
    XSRF_TOKEN_NAME = 'translation_download'
    REQUIRED_MODULES = [
        'inputex-string', 'inputex-select', 'inputex-hidden',
        'inputex-checkbox', 'inputex-list', 'gcb-uneditable',
        ]

    @classmethod
    def schema(cls):
        schema = schema_fields.FieldRegistry('Translation Download')
        schema.add_property(schema_fields.SchemaField(
            'export_what', 'Export Items', 'string',
            select_data=[
                ('new',
                 'Only items that are new or have out-of-date translations'),
                ('all', 'All translatable items')],
            description='Select what translation strings to export.'))

        locales_schema = schema_fields.FieldRegistry(
            None, description='locales')
        locales_schema.add_property(schema_fields.SchemaField(
            'locale', 'Locale', 'string', hidden=True, editable=False))
        locales_schema.add_property(schema_fields.SchemaField(
            'checked', None, 'boolean'))
        locales_schema.add_property(schema_fields.SchemaField(
            'title', None, 'string', optional=True, editable=False))

        schema.add_property(schema_fields.FieldArray(
            'locales', 'Languages', item_type=locales_schema,
            description='Select the languages whose translations you '
            'wish to export.',
            extra_schema_dict_values={
                'className': (
                    'inputEx-Field inputEx-ListField '
                    'label-group label-group-list')}))
        schema.add_property(schema_fields.SchemaField(
            'file_name', 'Download as File Named', 'string'))
        return schema

    def get(self):
        course = self.get_course()
        default_locale = course.default_locale
        locales = []
        for locale in course.all_locales:
            if locale == default_locale or locale == PSEUDO_LANGUAGE:
                continue
            locales.append({
                'locale': locale,
                'checked': True,
                'title': common_locales.get_locale_display_name(locale)})
        payload_dict = {
            'locales': locales,
            'file_name': course.title.lower().replace(' ', '_') + '.zip'
            }
        transforms.send_json_response(
            self, 200, 'Success.', payload_dict=payload_dict,
            xsrf_token=crypto.XsrfTokenManager.create_xsrf_token(
                self.XSRF_TOKEN_NAME))

    @staticmethod
    def build_translations(course, locales, export_what):
        """Build up a dictionary of all translated strings -> locale.

        For each {original-string,locale}, keep track of the course
        locations where this occurs, and each of the translations given.

        Args:
          course: The course for whose contents we are building translations.
          locales: Locales for which translations are desired.
          export_what: A string that tells us what should be added to the
            translations.  The value 'all' exports everything, translated
            or not, stale or not.  The value 'new' emits only things
            that have no translations, or whose translations are out-of-date
            with respect to the resource.
        Returns:
          Map of original-string -> locale -> TranslationsAndLocations instance.
        """

        app_context = course.app_context
        translations = collections.defaultdict(
            lambda: collections.defaultdict(TranslationsAndLocations))
        transformer = xcontent.ContentTransformer(
            config=I18nTranslationContext.get(app_context))
        resource_key_map = TranslatableResourceRegistry.get_resources_and_keys(
            course)

        # Preload all I18N progress DTOs; we'll need all of them.
        i18n_progress_dtos = I18nProgressDAO.get_all()
        progress_by_key = {p.id: p for p in i18n_progress_dtos}
        for locale in locales:
            # Preload all resource bundles for this locale; we need all of them.
            resource_bundle_dtos = ResourceBundleDAO.get_all_for_locale(locale)
            bundle_by_key = {b.id: b for b in resource_bundle_dtos}
            for rsrc, resource_key in resource_key_map:
                key = ResourceBundleKey(
                    resource_key.type, resource_key.key, locale)

                # If we don't already have a resource bundle, make it.
                resource_bundle_dto = bundle_by_key.get(str(key))
                if not resource_bundle_dto:
                    resource_bundle_dto = ResourceBundleDAO.create_blank(key)
                    resource_bundle_dtos.append(resource_bundle_dto)
                    bundle_by_key[resource_bundle_dto.id] = resource_bundle_dto

                # If we don't already have a progress record, make it.
                i18n_progress_dto = progress_by_key.get(str(resource_key))
                if not i18n_progress_dto:
                    i18n_progress_dto = I18nProgressDAO.create_blank(
                        resource_key)
                    i18n_progress_dtos.append(i18n_progress_dto)
                    progress_by_key[i18n_progress_dto.id] = i18n_progress_dto

                # Act as though we are loading the interactive translation
                # page and then clicking 'save'.  This has the side-effect of
                # forcing us to have created the resource bundle and progress
                # DTOs, and ensures that the operation here has identical
                # behavior with manual operation, and there are thus fewer
                # opportunities to go sideways and slip between the cracks.
                binding, sections = (
                    TranslationConsoleRestHandler.build_sections_for_key(
                        key, course, resource_bundle_dto, transformer))
                TranslationConsoleRestHandler.update_dtos_with_section_data(
                    key, sections, resource_bundle_dto, i18n_progress_dto)

                TranslationDownloadRestHandler._collect_section_translations(
                    translations, sections, binding, export_what, locale, key,
                    resource_key, rsrc)

            ResourceBundleDAO.save_all(resource_bundle_dtos)
        I18nProgressDAO.save_all(i18n_progress_dtos)
        return translations

    @staticmethod
    def _collect_section_translations(translations, sections, binding,
                                      export_what, locale, key, resource_key,
                                      rsrc):
        # For each section in the translation, make a record of that
        # in an internal data store which is used to generate .po
        # files.
        for section in sections:
            section_name = section['name']
            section_type = section['type']
            description = (
                binding.find_field(section_name).description or '')

            for translation in section['data']:
                message = unicode(translation['source_value'] or '')
                translated_message = translation['target_value'] or ''
                is_current = translation['verb'] == VERB_CURRENT
                old_message = translation['old_source_value']

                # Skip exporting blank items; pointless.
                if not message:
                    continue

                # If not exporting everything, and the current
                # translation is up-to-date, don't export it.
                if export_what != 'all' and is_current:
                    continue

                # Set source string and location.
                t_and_l = translations[message][locale]
                t_and_l.add_location('GCB-1|%s|%s|%s' % (
                    section_name, section_type, str(key)))

                # Describe the location where the item is found.
                t_and_l.add_comment(description)

                try:
                    resource_handler = resource.Registry.get(resource_key.type)
                    title = resource_handler.get_resource_title(rsrc)
                    if title:
                        t_and_l.add_comment(title)
                except AttributeError:
                    # Under ETL, there is no real handler and title lookup
                    # fails. In that case, we lose this data, which is non-
                    # essential.
                    pass

                # Add either the current translation (if current)
                # or the old translation as a remark (if we have one)
                if is_current:
                    t_and_l.add_translation(translated_message)
                else:
                    t_and_l.add_translation('')

                    if old_message:
                        t_and_l.set_previous_id(old_message)
                        if translated_message:
                            t_and_l.add_comment(
                                'Previously translated as: "%s"' %
                                translated_message)

    @staticmethod
    def build_babel_catalog_for_locale(course, translations, locale):
        environ = course.get_environ(course.app_context)
        course_title = environ['course'].get('title')
        bugs_address = environ['course'].get('admin_user_emails')
        organization = environ['base'].get('nav_header')
        with common_utils.ZipAwareOpen():
            # Load metadata for locale to which we are translating.
            localedata.load(locale)
        cat = catalog.Catalog(
            locale=locale,
            project='Translation for %s of %s' % (locale, course_title),
            msgid_bugs_address=bugs_address,
            copyright_holder=organization)
        for tr_id in translations:
            if locale in translations[tr_id]:
                t_and_l = translations[tr_id][locale]
                cat.add(
                    tr_id, string=t_and_l.translations.pop(),
                    locations=[(l, 0) for l in t_and_l.locations],
                    user_comments=t_and_l.comments,
                    auto_comments=['also translated as "%s"' % s
                                   for s in t_and_l.translations],
                    previous_id=t_and_l.previous_id)
        return cat

    @staticmethod
    def build_zip_file(course, out_stream, translations, locales):
        """Create a .zip file with one .po file for each translated language.

        Args:
          course: the Course object that we're building an export for.
          out_stream: An open file-like which can be written and seeked.
          translations: Map of string -> locale -> TranslationsAndLocations
            as returned from build_translations().
          locales: The set of locales for which we want to build .po files
        """
        app_context = course.app_context
        original_locale = app_context.default_locale
        with common_utils.ZipAwareOpen():
            # Load metadata for 'en', which Babel uses internally.
            localedata.load('en')
            # Load metadata for source language for course.
            localedata.load(original_locale)
        zf = zipfile.ZipFile(out_stream, 'w', allowZip64=True)
        try:
            for locale in locales:
                cat = (
                    TranslationDownloadRestHandler
                    .build_babel_catalog_for_locale(
                        course, translations, locale))
                filename = os.path.join(
                    'locale', locale, 'LC_MESSAGES', 'messages.po')
                content = cStringIO.StringIO()
                try:
                    pofile.write_po(content, cat, include_previous=True)
                    zf.writestr(filename, content.getvalue())
                finally:
                    content.close()
        finally:
            zf.close()

    def _send_response(self, out_stream, filename):
        self.response.content_type = 'application/octet-stream'
        self.response.content_disposition = (
            'attachment; filename="%s"' % filename)
        self.response.out.write(out_stream.getvalue())

    def _validate_inputs(self, course):
        if appengine_config.PRODUCTION_MODE:
            transforms.send_json_response(
                self, 403, 'Not available in production.')
            return None, None, None

        try:
            request = models.transforms.loads(self.request.get('request'))
        except ValueError:
            transforms.send_json_response(
                self, 400, 'Malformed or missing "request" parameter.')
            return None, None, None
        try:
            payload = models.transforms.loads(request.get('payload', ''))
        except ValueError:
            transforms.send_json_response(
                self, 400, 'Malformed or missing "payload" parameter.')
            return None, None, None
        if not self.assert_xsrf_token_or_fail(
            request, self.XSRF_TOKEN_NAME, {}):
            return None, None, None

        try:
            locales = [l['locale'] for l in payload.get('locales')
                       if l.get('checked') and l['locale'] != PSEUDO_LANGUAGE]
        except (TypeError, ValueError, KeyError):
            transforms.send_json_response(
                self, 400, 'Locales specification not as expected.')
            return None, None, None
        if not locales:
            # Nice UI message when no locales selected.
            transforms.send_json_response(
                self, 400, 'Please select at least one language to export.')
            return None, None, None
        for locale in locales:
            if not has_locale_rights(self.app_context, locale):
                transforms.send_json_response(self, 401, 'Access denied.')
                return None, None, None
        export_what = payload.get('export_what', 'new')
        file_name = payload.get(
            'file_name', course.title.lower().replace(' ', '_') + '.zip')
        return locales, export_what, file_name

    def put(self):
        """Verify inputs and return 200 OK to OEditor when all is well."""

        course = self.get_course()
        locales, _, _ = self._validate_inputs(course)
        if not locales:
            return
        transforms.send_json_response(self, 200, 'Success.')

    def post(self):
        """Actually generate the download content.

        This is a somewhat ugly solution to a somewhat ugly problem.
        The problem is this: The OEdtior form expects to see JSON
        responses, since it's meant for editing small well-structured
        objects.  Here, we're perverting that intent, and just using
        OEditor to present a form with options about the download.
        On successful "save", we have a hook that re-submits a form
        to hit the POST action, rather than the default PUT action,
        and that triggers the download.
        """

        course = self.get_course()
        locales, export_what, file_name = self._validate_inputs(course)
        if not locales:
            return

        translations = self.build_translations(course, locales, export_what)
        out_stream = StringIO.StringIO()
        # zip assumes stream has a real fp; fake it.
        out_stream.fp = out_stream
        try:
            self.build_zip_file(course, out_stream, translations, locales)
            self._send_response(out_stream, file_name)
        finally:
            out_stream.close()


class I18nUploadHandler(BaseDashboardExtension):
    ACTION = 'i18n_upload'

    def render(self):
        main_content = oeditor.ObjectEditor.get_html_for(
            self.handler,
            TranslationUploadRestHandler.SCHEMA.get_json_schema(),
            TranslationUploadRestHandler.SCHEMA.get_schema_dict(),
            '',
            self.handler.canonicalize_url(TranslationUploadRestHandler.URL),
            self.handler.get_action_url(I18nDashboardHandler.ACTION),
            required_modules=TranslationUploadRestHandler.REQUIRED_MODULES,
            save_method='upload', save_button_caption='Upload',
            extra_js_files=['upload_translations.js'],
            additional_dirs=[TEMPLATES_DIR])
        self.handler.render_page({
            'page_title': self.handler.format_title('I18n Translation Upload'),
            'main_content': main_content,
            }, in_action=I18nDashboardHandler.ACTION)


def translation_upload_generate_schema():
    schema = schema_fields.FieldRegistry('Translation Upload')
    schema.add_property(schema_fields.SchemaField(
        'file', 'Translation File', 'file',
        # Not really optional, but oeditor marks un-filled mandatory field as
        # an error, and doesn't un-mark when the user has selected a file, so
        # cleaner to just not mark as error and catch missing files on
        # PUT/POST with a nice error message, which we had to do anyhow.
        optional=True,
        description='Use this option to nominate a .po file containing '
        'translations for a single language, or a .zip file containing '
        'multiple translated languages.  The internal structure of the .zip '
        'file is unimportant; all files ending in ".po" will be considered.'))
    return schema


class TranslationUploadRestHandler(utils.BaseRESTHandler):
    URL = '/rest/modules/i18n_dashboard/upload'
    XSRF_TOKEN_NAME = 'translation-upload'
    SCHEMA = translation_upload_generate_schema()
    REQUIRED_MODULES = ['inputex-hidden', 'inputex-select', 'inputex-string',
                        'gcb-uneditable', 'inputex-file',
                        'io-upload-iframe']

    class ProtocolError(Exception):
        pass

    def get(self):
        transforms.send_json_response(
            self, 200, 'Success.', payload_dict={'key': None},
            xsrf_token=crypto.XsrfTokenManager.create_xsrf_token(
                self.XSRF_TOKEN_NAME))

    @staticmethod
    def build_translations_defaultdict():
        # Build up set of all incoming translations as a nested dict:
        # locale -> bundle key -> {'original text': 'translated text'}
        return collections.defaultdict(lambda: collections.defaultdict(dict))

    @staticmethod
    def parse_po_file(translations, po_file_content):
        """Collect translations from .po file and group by bundle key."""

        pseudo_file = cStringIO.StringIO(po_file_content)
        the_catalog = pofile.read_po(pseudo_file)
        locale = None
        for message in the_catalog:
            for location, _ in message.locations:
                protocol, _, _, key = location.split('|', 4)
                if protocol != 'GCB-1':
                    raise TranslationUploadRestHandler.ProtocolError(
                        'Expected location format GCB-1, but had %s' % protocol)

                message_locale = ResourceBundleKey.fromstring(key).locale
                if locale is None:
                    locale = message_locale
                elif locale != message_locale:
                    raise TranslationUploadRestHandler.ProtocolError(
                        'File has translations for both "%s" and "%s"' % (
                            locale, message_locale))

                translations[locale][key][message.id] = message.string

    @staticmethod
    def update_translations(course, translations, messages):
        app_context = course.app_context
        transformer = xcontent.ContentTransformer(
            config=I18nTranslationContext.get(app_context))
        i18n_progress_dtos = I18nProgressDAO.get_all()
        progress_by_key = {p.id: p for p in i18n_progress_dtos}
        resource_key_map = TranslatableResourceRegistry.get_resources_and_keys(
            course)

        for locale, resource_translations in translations.iteritems():
            used_resource_translations = set()
            num_resources = 0
            num_replacements = 0
            num_blank_translations = 0
            resource_bundle_dtos = ResourceBundleDAO.get_all_for_locale(locale)
            bundle_by_key = {b.id: b for b in resource_bundle_dtos}

            for _, resource_key in resource_key_map:
                num_resources += 1
                key = ResourceBundleKey(
                    resource_key.type, resource_key.key, locale)
                key_str = str(key)

                # Here, be permissive: just create the bundle or progress DTO
                # if it does not currently exist.  Guaranteed we won't have
                # translations for this resource, since we'd have created the
                # bundle on export, but this makes us 1:1 with the behavior on
                # manual edit and on export.
                resource_bundle_dto = bundle_by_key.get(key_str)
                if not resource_bundle_dto:
                    resource_bundle_dto = ResourceBundleDAO.create_blank(key)
                    resource_bundle_dtos.append(resource_bundle_dto)
                    bundle_by_key[resource_bundle_dto.id] = resource_bundle_dto

                i18n_progress_dto = progress_by_key.get(str(key.resource_key))
                if not i18n_progress_dto:
                    i18n_progress_dto = I18nProgressDAO.create_blank(
                        resource_key)
                    i18n_progress_dtos.append(i18n_progress_dto)
                    progress_by_key[i18n_progress_dto.id] = i18n_progress_dto

                translations = resource_translations.get(key_str)
                if translations:
                    used_resource_translations.add(key_str)
                else:
                    # Even though we don't have translations for this resource,
                    # keep going; we want to update the progress DTO below.
                    translations = {}

                used_translations = set()
                _, sections = (
                    TranslationConsoleRestHandler.build_sections_for_key(
                        key, course, resource_bundle_dto, transformer))
                for section in sections:
                    for item in section['data']:
                        source_value = item['source_value']
                        if not isinstance(source_value, basestring):
                            source_value = unicode(source_value)  # convert num
                        if source_value not in translations:
                            messages.append(
                                'Did not find translation for "%s"' %
                                source_value)
                        elif translations[source_value]:
                            item['target_value'] = translations[source_value]
                            item['changed'] = True
                            used_translations.add(source_value)
                            num_replacements += 1
                        else:
                            used_translations.add(source_value)
                            num_blank_translations += 1

                for unused_translation in set(translations) - used_translations:
                    messages.append(
                        'Translation for "%s" present but not used.' %
                        unused_translation)

                TranslationConsoleRestHandler.update_dtos_with_section_data(
                    key, sections, resource_bundle_dto, i18n_progress_dto)

            for unused in (
                set(resource_translations) - used_resource_translations):

                messages.append(
                    ('Translation file had %d items for resource "%s", but '
                     'course had no such resource.') % (
                         len(resource_translations[unused]), unused))
            messages.append(
                ('For %s, made %d total replacements in %d resources.  '
                 '%d items in the uploaded file did not have translations.') % (
                    common_locales.get_locale_display_name(locale),
                    num_replacements, num_resources, num_blank_translations))
            ResourceBundleDAO.save_all(resource_bundle_dtos)
        I18nProgressDAO.save_all(i18n_progress_dtos)

    def post(self):
        if appengine_config.PRODUCTION_MODE:
            transforms.send_json_response(
                self, 403, 'Not available in production.')
            return

        try:
            request = models.transforms.loads(self.request.get('request'))
        except ValueError:
            transforms.send_file_upload_response(
                self, 400, 'Malformed or missing "request" parameter.')
            return
        token = request.get('xsrf_token')
        if not token or not crypto.XsrfTokenManager.is_xsrf_token_valid(
            token, self.XSRF_TOKEN_NAME):

            transforms.send_file_upload_response(
                self, 403, 'Missing or invalid XSRF token.')
            return
        if 'file' not in self.request.POST:
            transforms.send_file_upload_response(
                self, 400, 'Must select a .zip or .po file to upload.')
            return

        upload = self.request.POST['file']
        if not isinstance(upload, cgi.FieldStorage):
            transforms.send_file_upload_response(
                self, 400, 'Must select a .zip or .po file to upload')
            return
        file_content = upload.file.read()
        if not file_content:
            transforms.send_file_upload_response(
                self, 400, 'The .zip or .po file must not be empty.')
            return

        # Get meta-data for supported locales loaded.  Need to do this before
        # attempting to parse .po file content.  Do this now, since we don't
        # rely on file names to establish locale, just bundle keys.  Since
        # bundle keys are in .po file content, and since we need locales
        # loaded to parse file content, resolve recursion by pre-emptively
        # just grabbing everything.
        for locale in self.app_context.get_all_locales():
            with common_utils.ZipAwareOpen():
                localedata.load(locale)

        # Build up set of all incoming translations as a nested dict:
        # locale -> bundle key -> {'original text': 'translated text'}
        translations = self.build_translations_defaultdict()
        try:
            try:
                zf = zipfile.ZipFile(cStringIO.StringIO(file_content), 'r')
                for item in zf.infolist():
                    if item.filename.endswith('.po'):
                        self.parse_po_file(translations, zf.read(item))
            except zipfile.BadZipfile:
                try:
                    self.parse_po_file(translations, file_content)
                except UnicodeDecodeError:
                    transforms.send_file_upload_response(
                        self, 400,
                        'Uploaded file did not parse as .zip or .po file.')
                    return
        except TranslationUploadRestHandler.ProtocolError, ex:
            transforms.send_file_upload_response(self, 400, str(ex))
            return

        if not translations:
            transforms.send_file_upload_response(
                self, 400, 'No translations found in provided file.')
            return

        for locale in translations:
            if not has_locale_rights(self.app_context, locale):
                transforms.send_file_upload_response(
                    self, 401, 'Access denied.')
                return

        messages = []
        self.update_translations(self.get_course(), translations, messages)
        transforms.send_file_upload_response(
            self, 200, 'Success.', payload_dict={'messages': messages})


class I18nProgressManager(caching.RequestScopedSingleton):

    def __init__(self, course):
        self._course = course
        self._key_to_progress = None

    def _preload(self):
        self._key_to_progress = {}
        for row in I18nProgressDAO.get_all_iter():
            self._key_to_progress[str(resource.Key.fromstring(row.id))] = row

    def _get(self, rsrc, type_str, key):
        if self._key_to_progress is None:
            self._preload()
        resource_key = resource.Key(type_str, key)
        return ResourceRow(
            self._course, rsrc, type_str, key,
            i18n_progress_dto=self._key_to_progress.get(str(resource_key)),
            resource_key=resource_key)

    @classmethod
    def get(cls, course, rsrc, type_str, key):
        # pylint: disable=protected-access
        return cls.instance(course)._get(rsrc, type_str, key)


# all caches must have limits
MAX_GLOBAL_CACHE_SIZE_BYTES = 16 * 1024 * 1024

# we don't track deletions; deleted item will hang around this long
CACHE_ENTRY_TTL_SEC = 5 * 60

# Global memcache controls.
CAN_USE_RESOURCE_BUNDLE_IN_PROCESS_CACHE = ConfigProperty(
    'gcb_can_use_resource_bundle_in_process_cache', bool, (
        'Whether or not to cache I18N translations. For production this value '
        'should be on to enable maximum performance. For development this '
        'value should be off so you can see your changes to course content '
        'instantaneously.'), default_value=True,
    label='Cache internationalization')


class ProcessScopedResourceBundleCache(caching.ProcessScopedSingleton):
    """This class holds in-process global cache of VFS objects."""

    @classmethod
    def get_cache_len(cls):
        # pylint: disable=protected-access
        return len(
            ProcessScopedResourceBundleCache.instance()._cache.items.keys())

    @classmethod
    def get_cache_size(cls):
        # pylint: disable=protected-access
        return ProcessScopedResourceBundleCache.instance()._cache.total_size

    def __init__(self):
        self._cache = caching.LRUCache(
            max_size_bytes=MAX_GLOBAL_CACHE_SIZE_BYTES)
        self._cache.get_entry_size = self._get_entry_size

    def _get_entry_size(self, key, value):
        return sys.getsizeof(key) + sys.getsizeof(value) if value else 0

    @property
    def cache(self):
        return self._cache


class ResourceBundleCacheEntry(caching.AbstractCacheEntry):
    """Cache entry representing a file."""

    def __init__(self, entity):
        self.entity = entity
        self.created_on = datetime.datetime.utcnow()

    def getsizeof(self):
        return (
            ResourceBundleEntity.getsizeof(self.entity) +
            sys.getsizeof(self.created_on))

    def has_expired(self):
        age = (datetime.datetime.utcnow() - self.created_on).total_seconds()
        return age > CACHE_ENTRY_TTL_SEC

    def is_up_to_date(self, key, update):
        if update and self.entity:
            return update.updated_on == self.entity.updated_on
        return not update and not self.entity

    def updated_on(self):
        if self.entity:
            return self.entity.updated_on
        return None

    @classmethod
    def externalize(cls, key, entry):
        entity = entry.entity
        if not entity:
            return None
        return ResourceBundleDAO.DTO(
            entity.key().id_or_name(), transforms.loads(entity.data))

    @classmethod
    def internalize(cls, key, entity):
        return cls(entity)


class ResourceBundleCacheConnection(caching.AbstractCacheConnection):

    PERSISTENT_ENTITY = ResourceBundleEntity
    CACHE_ENTRY = ResourceBundleCacheEntry

    @classmethod
    def init_counters(cls):
        super(ResourceBundleCacheConnection, cls).init_counters()

        cls.CACHE_INHERITED = PerfCounter(
            'gcb-models-ResourceBundleConnection-cache-inherited',
            'A number of times an object was obtained from the inherited vfs.')

    @classmethod
    def is_enabled(cls):
        return CAN_USE_RESOURCE_BUNDLE_IN_PROCESS_CACHE.value

    def __init__(self, namespace):
        super(ResourceBundleCacheConnection, self).__init__(namespace)
        self.cache = ProcessScopedResourceBundleCache.instance().cache

    def get_updates_when_empty(self):
        """Load in all ResourceBundles when cache is empty."""
        q = self.PERSISTENT_ENTITY.all()
        for entity in caching.iter_all(q):
            self.put(entity.key().name(), entity)
            self.CACHE_UPDATE_COUNT.inc()

        # we don't have any updates to apply; all items are new
        return {}


RB_CACHE_LEN = models.counters.PerfCounter(
    'gcb-models-ResourceBundleCacheConnection-cache-len',
    'A total number of items in cache.')
RB_CACHE_SIZE_BYTES = PerfCounter(
    'gcb-models-ResourceBundleCacheConnection-cache-bytes',
    'A total size of items in cache in bytes.')

RB_CACHE_LEN.poll_value = ProcessScopedResourceBundleCache.get_cache_len
RB_CACHE_SIZE_BYTES.poll_value = (
    ProcessScopedResourceBundleCache.get_cache_size)


ResourceBundleCacheConnection.init_counters()


class I18nResourceBundleManager(caching.RequestScopedSingleton):
    """Class that provides access to in-process ResourceBundle cache.

    This class only supports get() and does not intercept put() or delete()
    and is unaware of changes to ResourceBundles made in this very process.
    When ResourceBundles change, the changes will be picked up when new instance
    of this class is created. If you are watching perfomance counters, you will
    see EVICT and EXPIRE being incremented, but not DELETE or PUT.
    """

    def __init__(self, namespace):
        self._conn = ResourceBundleCacheConnection.new_connection(namespace)

    def _get(self, key):
        found, stream = self._conn.get(key)
        if found and stream:
            return stream
        entity = ResourceBundleDAO.ENTITY_KEY_TYPE.get_entity_by_key(
            ResourceBundleEntity, str(key))
        if entity:
            self._conn.put(key, entity)
            return ResourceBundleDAO.DTO(
                entity.key().id_or_name(), transforms.loads(entity.data))
        self._conn.CACHE_NOT_FOUND.inc()
        self._conn.put(key, None)
        return None

    def _get_multi(self, keys):
        return [self._get(key) for key in keys]

    @classmethod
    def get(cls, app_context, key):
        # pylint: disable=protected-access
        return cls.instance(app_context.get_namespace_name())._get(key)

    @classmethod
    def get_multi(cls, app_context, keys):
        # pylint: disable=protected-access
        return cls.instance(
            app_context.get_namespace_name())._get_multi(keys)


class I18nTranslationContext(caching.RequestScopedSingleton):

    def __init__(self, app_context):
        self.app_context = app_context
        self._xcontent_config = None

    @classmethod
    def _init_xcontent_configuration(cls, app_context):
        inline_tag_names = list(xcontent.DEFAULT_INLINE_TAG_NAMES)
        opaque_decomposable_tag_names = list(
            xcontent.DEFAULT_OPAQUE_DECOMPOSABLE_TAG_NAMES)
        recomposable_attributes_map = dict(
            xcontent.DEFAULT_RECOMPOSABLE_ATTRIBUTES_MAP)
        recomposable_attributes_map['HREF'] = {'A'}

        for tag_name, tag_cls in tags.Registry.get_all_tags().items():
            tag_schema = None
            try:
                tag_schema = tag_cls().get_schema(None)
            except Exception:  # pylint: disable=broad-except
                logging.exception('Cannot get schema for %s', tag_name)
                continue

            index = schema_fields.FieldRegistryIndex(tag_schema)
            index.rebuild()

            for name in (
                TRANSLATABLE_FIELDS_FILTER.filter_field_registry_index(index)
            ):
                inline_tag_names.append(tag_name.upper())
                opaque_decomposable_tag_names.append(tag_name.upper())
                recomposable_attributes_map.setdefault(
                    name.upper(), set()).add(tag_name.upper())

        return xcontent.Configuration(
            inline_tag_names=inline_tag_names,
            opaque_decomposable_tag_names=opaque_decomposable_tag_names,
            recomposable_attributes_map=recomposable_attributes_map,
            omit_empty_opaque_decomposable=False,
            sort_attributes=True)

    def _get_xcontent_configuration(self):
        if self._xcontent_config is None:
            self._xcontent_config = self._init_xcontent_configuration(
                self.app_context)
        return self._xcontent_config

    @classmethod
    def get(cls, app_context):
        # pylint: disable=protected-access
        return cls.instance(app_context)._get_xcontent_configuration()


def swapcase(text):
    """Swap case for full words with only alpha/num and punctutation marks."""

    def swap(root):
        for node in root.childNodes:
            if node.nodeType == minidom.Node.TEXT_NODE:
                if node.nodeValue:
                    text = node.nodeValue.swapcase()

                    # revert swapping of formatting %(...){s, f, ...}
                    text = re.sub(
                        r'\%(\([a-zA-Z]*\))?[DIEeFfS]',
                        lambda m: m.group().swapcase(), text)

                    # add lambda character at the end to test all code paths
                    # properly handle a multibyte character in the content
                    node.nodeValue = text + unichr(0x03BB)
            if node.nodeType == minidom.Node.ELEMENT_NODE:
                swap(node)

    try:
        tree = xcontent.TranslationIO.fromstring(text)
        swap(tree.documentElement)
        return xcontent.TranslationIO.tostring(tree)
    except:  # pylint: disable=bare-except
        logging.exception('Failed swapcase() for: %s', text)
        return text


class I18nReverseCaseHandler(BaseDashboardExtension):
    """Provide "translation" that swaps case of letters."""

    ACTION = 'i18n_reverse_case'

    @classmethod
    def translate_course(cls, course):
        """Translates a course to rEVERSED cAPS.

        Args:
          course: The course for whose contents we are making translations.

        Returns:
          None.
        """

        cls._add_reverse_case_locale(course)
        po_file_content = cls._build_reverse_case_translations(course)
        cls._set_reverse_case_translations(course, po_file_content)

    @staticmethod
    def _add_reverse_case_locale(course):
        environ = course.get_environ(course.app_context)
        extra_locales = environ.setdefault('extra_locales', [])
        if not any(
                l[courses.Course.SCHEMA_LOCALE_LOCALE] == PSEUDO_LANGUAGE
                for l in extra_locales):
            extra_locales.append({
                courses.Course.SCHEMA_LOCALE_LOCALE: PSEUDO_LANGUAGE,
                courses.Course.SCHEMA_LOCALE_AVAILABILITY: (
                    courses.Course.SCHEMA_LOCALE_AVAILABILITY_UNAVAILABLE)})
            course.save_settings(environ)

    @staticmethod
    def _build_reverse_case_translations(course):
        original_locale = course.app_context.default_locale
        with common_utils.ZipAwareOpen():
            # Load metadata for 'en', which Babel uses internally.
            localedata.load('en')
            # Load metadata for base course language.
            localedata.load(original_locale)

        translations = TranslationDownloadRestHandler.build_translations(
            course, [PSEUDO_LANGUAGE], 'all')
        cat = TranslationDownloadRestHandler.build_babel_catalog_for_locale(
            course, translations, PSEUDO_LANGUAGE)
        for message in cat:
            message.string = swapcase(message.id)
        try:
            content = cStringIO.StringIO()
            pofile.write_po(content, cat)
            return content.getvalue()
        finally:
            content.close()

    @staticmethod
    def _set_reverse_case_translations(course, po_file_content):
        translations = (
            TranslationUploadRestHandler.build_translations_defaultdict())
        TranslationUploadRestHandler.parse_po_file(
            translations, po_file_content)
        messages = []
        TranslationUploadRestHandler.update_translations(course, translations,
                                                         messages)
        for message in messages:
            logging.warning(message)

    def render(self):
        course = self.handler.get_course()
        self.translate_course(course)
        self.handler.redirect(
            self.handler.get_action_url(I18nDashboardHandler.ACTION))


class AbstractTranslatableResourceType(object):

    @classmethod
    def get_ordering(cls):
        raise NotImplementedError('Derived classes must implement this.')

    @classmethod
    def get_title(cls):
        raise NotImplementedError('Derived classes must implement this.')

    @classmethod
    def get_resources_and_keys(cls, course):
        raise NotImplementedError('Derived classes must implement this.')


class TranslatableResourceRegistry(object):

    ORDERING_FIRST = 0
    ORDERING_EARLY = 3
    ORDERING_MIDDLE = 5
    ORDERING_LATE = 8
    ORDERING_LAST = 10

    _RESOURCE_TYPES = []
    _RESOURCE_TITLES = set()

    @classmethod
    def register(cls, translatable_resource):
        title = translatable_resource.get_title()
        if title in cls._RESOURCE_TITLES:
            raise ValueError(
                'Title "%s" is already registered as a translatable resource.' %
                title)
        cls._RESOURCE_TITLES.add(title)
        cls._RESOURCE_TYPES.append(translatable_resource)

    @classmethod
    def get_all(cls):
        return [x for x in sorted(cls._RESOURCE_TYPES,
                                  key=lambda x: x.get_ordering())]

    @classmethod
    def get_resources_and_keys(cls, course):
        ret = []
        for resource_type in cls.get_all():
            ret += resource_type.get_resources_and_keys(course)
        return ret


class TranslatableResourceCourseSettings(AbstractTranslatableResourceType):

    @classmethod
    def get_ordering(cls):
        return TranslatableResourceRegistry.ORDERING_FIRST

    @classmethod
    def get_title(cls):
        return 'Course Settings'

    @classmethod
    def get_resources_and_keys(cls, course):
        ret = []
        for section_name in sorted(courses.Course.get_schema_sections()):
            ret.append((
                resources_display.ResourceCourseSettings.get_resource(
                    course, section_name),
                resource.Key(resources_display.ResourceCourseSettings.TYPE,
                    section_name, course),
                ))
        return ret


class TranslatableResourceCourseComponents(AbstractTranslatableResourceType):

    @classmethod
    def get_ordering(cls):
        return TranslatableResourceRegistry.ORDERING_MIDDLE

    @classmethod
    def get_title(cls):
        return 'Course Outline'

    @classmethod
    def get_resources_and_keys(cls, course):
        ret = []
        for unit in course.get_units():
            if course.get_parent_unit(unit.unit_id):
                continue
            if unit.is_custom_unit():
                key = custom_units.UnitTypeRegistry.i18n_resource_key(
                    course, unit)
                if key:
                    ret.append((unit, key))
            else:
                ret.append(
                    (unit, resources_display.ResourceUnitBase.key_for_unit(
                        unit, course)))
                if unit.type == verify.UNIT_TYPE_UNIT:
                    if unit.pre_assessment:
                        assessment = course.find_unit_by_id(unit.pre_assessment)
                        ret.append(
                            (assessment,
                             resource.Key(
                                 resources_display.ResourceAssessment.TYPE,
                                 unit.pre_assessment, course)))
                    for lesson in course.get_lessons(unit.unit_id):
                        ret.append(((unit, lesson),
                                    resource.Key(
                                        resources_display.ResourceLesson.TYPE,
                                        lesson.lesson_id, course)))
                    if unit.post_assessment:
                        assessment = course.find_unit_by_id(
                            unit.post_assessment)
                        ret.append(
                            (assessment,
                             resource.Key(
                                 resources_display.ResourceAssessment.TYPE,
                                 unit.post_assessment, course)))
        return ret


class TranslatableResourceQuestions(AbstractTranslatableResourceType):

    @classmethod
    def get_ordering(cls):
        return TranslatableResourceRegistry.ORDERING_LATE

    @classmethod
    def get_title(cls):
        return 'Questions'

    @classmethod
    def get_resources_and_keys(cls, course):
        ret = []
        for qu in models.QuestionDAO.get_all():
            ret.append((qu, resource.Key(
                resources_display.ResourceQuestionBase.get_question_key_type(
                    qu), qu.id, course)))
        return ret


class TranslatableResourceQuestionGroups(AbstractTranslatableResourceType):

    @classmethod
    def get_ordering(cls):
        return TranslatableResourceRegistry.ORDERING_LATE

    @classmethod
    def get_title(cls):
        return 'Question Groups'

    @classmethod
    def get_resources_and_keys(cls, course):
        ret = []
        for qg in models.QuestionGroupDAO.get_all():
            ret.append((qg, resource.Key(
                resources_display.ResourceQuestionGroup.TYPE, qg.id, course)))
        return ret


class TranslatableResourceHtmlHooks(AbstractTranslatableResourceType):

    @classmethod
    def get_ordering(cls):
        return TranslatableResourceRegistry.ORDERING_LAST

    @classmethod
    def get_title(cls):
        return 'HTML Hooks'

    @classmethod
    def get_resources_and_keys(cls, course):
        ret = [(v, k)
               for k, v in utils.ResourceHtmlHook.get_all(course).iteritems()]
        ret.sort(key=lambda row: row[0][utils.ResourceHtmlHook.NAME])
        return ret


class I18nDashboardHandler(BaseDashboardExtension):
    """Provides the logic for rendering the i18n workflow dashboard."""

    ACTION = 'i18n_dashboard'

    def __init__(self, handler):
        super(I18nDashboardHandler, self).__init__(handler)
        self.course = handler.get_course()
        all_locales = self.handler.app_context.get_all_locales()
        self.main_locale = all_locales[0]
        self.extra_locales = all_locales[1:]

    def _make_table_section(self, data_rows, section_title):
        rows = []
        rows.append(EmptyRow(name='', class_name='blank-row'))
        rows.append(SectionRow(section_title))
        if data_rows:
            rows += data_rows
        else:
            rows.append(EmptyRow())
        return rows

    def render(self):
        rows = []

        for resource_handler in TranslatableResourceRegistry.get_all():
            data_rows = []
            for rsrc, key in resource_handler.get_resources_and_keys(
                self.course):
                data_rows.append(I18nProgressManager.get(
                    self.course, rsrc, key.type, key.key))
            rows += self._make_table_section(
                data_rows, resource_handler.get_title())

        if not [row for row in rows if type(row) is ResourceRow]:
            rows = [EmptyRow(name='No course content')]

        permitted_locales = []
        for locale in self.extra_locales:
            if roles.Roles.is_user_allowed(
                self.handler.app_context, custom_module,
                locale_to_permission(locale)
            ):
                permitted_locales.append(locale)

        template_values = {
            'extra_locales': permitted_locales,
            'rows': rows,
            'num_columns': len(permitted_locales) + 1,
            'is_readonly': self.is_readonly(self.course),
        }

        if roles.Roles.is_course_admin(self.handler.app_context):
            template_values['main_locale'] = self.main_locale
            template_values['is_translatable_xsrf_token'] = (
                crypto.XsrfTokenManager.create_xsrf_token(
                    IsTranslatableRestHandler.XSRF_TOKEN_NAME))
            template_values['num_columns'] += 1

        main_content = self.handler.get_template(
            'i18n_dashboard.html', [TEMPLATES_DIR]).render(template_values)
        edit_actions = [
            {
                'id': 'delete_translation',
                'caption': 'Delete Translations',
                'href': self.handler.get_action_url(
                    I18nDeletionHandler.ACTION),
                },
            {
                'id': 'upload_translation_files',
                'caption': 'Upload Translation Files',
                'href': self.handler.get_action_url(
                    I18nUploadHandler.ACTION),
                },
            {
                'id': 'download_translation_files',
                'caption': 'Download Translation Files',
                'href': self.handler.get_action_url(
                    I18nDownloadHandler.ACTION),
                },
            ]

        translate_actions = [
            {
                'id': 'translate_to_reverse_case',
                'caption': '"Translate" to rEVERSED cAPS',
                'href': self.handler.get_action_url(
                    I18nReverseCaseHandler.ACTION),
                },
            ]
        actions = []
        if not self.is_readonly(self.course):
            actions += translate_actions
            if len(self.course.all_locales) > 1:
                actions += edit_actions
        if appengine_config.PRODUCTION_MODE:
            message = (
                'This operation takes a substantial amount of time, and '
                'is very likely to time out when executed from a web '
                'browser talking to a production server.  Alternatives '
                'are to work with a development server or use the ETL '
                'scripts to do translation upload/download.  See the '
                'file .../scripts/etl.sh in your Course Builder download for '
                'complete instructions on usage. ')
            disabled_alert = 'javascript: alert("%s")' % message
            for action in actions:
                del action['href']
                action['action'] = disabled_alert

        if self.handler.can_view('settings_i18n'):
            actions += [
                {
                    'id': 'edit_18n_settings',
                    'caption': 'Edit I18N Settings',
                    'href': self.handler.get_action_url(
                        'settings_i18n', extra_args={
                            'exit_url': 'dashboard?action=' + self.ACTION,
                        })
                    },
                ]

        self.handler.render_page({
            'page_title': self.handler.format_title('I18n Workflow'),
            'main_content': jinja2.utils.Markup(main_content),
            'sections': [{
                    'actions': actions,
                    'pre': ' ',
                    }]
            })


class TranslationConsole(BaseDashboardExtension):
    ACTION = 'i18_console'

    @classmethod
    def get_edit_url(cls, key):
        return 'dashboard?%s' % urllib.urlencode({
            'action': cls.ACTION,
            'key': key})

    def render(self):
        main_content = oeditor.ObjectEditor.get_html_for(
            self.handler,
            TranslationConsoleRestHandler.SCHEMA.get_json_schema(),
            TranslationConsoleRestHandler.SCHEMA.get_schema_dict(),
            self.handler.request.get('key'),
            self.handler.canonicalize_url(TranslationConsoleRestHandler.URL),
            self.handler.get_action_url(I18nDashboardHandler.ACTION),
            auto_return=False,
            required_modules=TranslationConsoleRestHandler.REQUIRED_MODULES,
            extra_css_files=['translation_console.css'],
            extra_js_files=['translation_console.js'],
            additional_dirs=[TEMPLATES_DIR])

        if self.is_readonly(self.handler.get_course()):
            main_content = self.format_readonly_message()

        self.handler.render_page({
            'page_title': self.handler.format_title('I18n Workflow'),
            'main_content': main_content,
            }, in_action=I18nDashboardHandler.ACTION)


def tc_generate_schema():
    schema = schema_fields.FieldRegistry(
        'Translation Console', extra_schema_dict_values={
            'className': 'inputEx-Group translation-console'})

    schema.add_property(schema_fields.SchemaField(
        'title', 'Title', 'string', editable=False))

    schema.add_property(schema_fields.SchemaField(
        'key', 'ID', 'string', hidden=True))
    schema.add_property(schema_fields.SchemaField(
        'source_locale', 'Source Locale', 'string', hidden=True))
    schema.add_property(schema_fields.SchemaField(
        'target_locale', 'Target Locale', 'string', hidden=True))

    section = schema_fields.FieldRegistry(
        None, 'section', extra_schema_dict_values={
            'className': 'inputEx-Group translation-item'})
    section.add_property(schema_fields.SchemaField(
        'name', '', 'string', hidden=True))
    section.add_property(schema_fields.SchemaField(
        'label', 'Name', 'string', editable=False))
    section.add_property(schema_fields.SchemaField(
        'type', 'Type', 'string', editable=False, optional=True))
    section.add_property(schema_fields.SchemaField(
        'source_value', 'source_value', 'string', hidden=True, optional=True))

    item = schema_fields.FieldRegistry(None, 'item')
    item.add_property(schema_fields.SchemaField(
        'source_value', 'Original', 'string', optional=True,
        extra_schema_dict_values={'_type': 'text', 'className': 'disabled'}))
    item.add_property(schema_fields.SchemaField(
        'target_value', 'Translated', 'string', optional=True,
        extra_schema_dict_values={'_type': 'text', 'className': 'active'}))
    item.add_property(schema_fields.SchemaField(
        'verb', 'Verb', 'number', hidden=True, optional=True))
    item.add_property(schema_fields.SchemaField(
        'old_source_value', 'Old Source Value', 'string', hidden=True,
        optional=True))
    item.add_property(schema_fields.SchemaField(
        'changed', 'Changed', 'boolean', hidden=True, optional=True))

    section.add_property(schema_fields.FieldArray(
        'data', 'Data', item_type=item,
        extra_schema_dict_values={}))

    schema.add_property(schema_fields.FieldArray(
        'sections', 'Sections', item_type=section))

    return schema


class TranslationConsoleRestHandler(utils.BaseRESTHandler):
    URL = '/rest/modules/i18n_dashboard/translation_console'
    XSRF_TOKEN_NAME = 'translation-console'

    SCHEMA = tc_generate_schema()

    REQUIRED_MODULES = [
        'inputex-hidden', 'inputex-list', 'inputex-string', 'inputex-textarea',
        'gcb-uneditable']

    def get(self):

        def cmp_sections(section1, section2):
            """Comparator to sort the sections in schema order."""
            name1 = section1['name']
            name2 = section2['name']
            path1 = name1.split(':')
            path2 = name2.split(':')
            for part1, part2 in zip(path1, path2):
                if part1[0] == '[' and part1[-1] == ']':
                    assert part2[0] == '[' and part2[-1] == ']'
                    c = cmp(int(part1[1:-1]), int(part2[1:-1]))
                    if c != 0:
                        return c
                    else:
                        continue
                elif part1 != part2:
                    name_no_index1, _ = (
                        schema_fields.FieldRegistry.compute_name(path1))
                    name_no_index2, _ = (
                        schema_fields.FieldRegistry.compute_name(path2))
                    return cmp(
                        binding.index.names_in_order.index(name_no_index1),
                        binding.index.names_in_order.index(name_no_index2))
            return cmp(len(path1), len(path2))

        key = ResourceBundleKey.fromstring(self.request.get('key'))
        if not has_locale_rights(self.app_context, key.locale):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': str(key)})
            return

        resource_bundle_dto = I18nResourceBundleManager.get(
            self.app_context, str(key))
        transformer = xcontent.ContentTransformer(
            config=I18nTranslationContext.get(self.app_context))
        course = self.get_course()
        binding, sections = self.build_sections_for_key(
            key, course, resource_bundle_dto, transformer)
        resource_key = key.resource_key
        resource_handler = resource.Registry.get(resource_key.type)
        rsrc = resource_handler.get_resource(course, resource_key.key)
        title = _build_resource_title(self.app_context, resource_key.type, rsrc)
        payload_dict = {
            'key': str(key),
            'title': unicode(title),
            'source_locale': self.app_context.default_locale,
            'target_locale': key.locale,
            'sections': sorted(sections, cmp=cmp_sections)
        }

        transforms.send_json_response(
            self, 200, 'Success.',
            payload_dict=payload_dict,
            xsrf_token=crypto.XsrfTokenManager.create_xsrf_token(
                self.XSRF_TOKEN_NAME))

    def put(self):
        request = transforms.loads(self.request.get('request'))
        key = ResourceBundleKey.fromstring(request['key'])
        validate = request.get('validate', False)

        if not self.assert_xsrf_token_or_fail(
                request, self.XSRF_TOKEN_NAME, {'key': str(key)}):
            return

        if not has_locale_rights(self.app_context, key.locale):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': str(key)})
            return

        payload = transforms.loads(request['payload'])
        payload_dict = transforms.json_to_dict(
            payload, self.SCHEMA.get_json_schema_dict())

        # Update the resource bundle
        resource_bundle_dto = ResourceBundleDAO.load_or_default(key)
        i18n_progress_dto = I18nProgressDAO.load_or_default(key.resource_key)
        self.update_dtos_with_section_data(
            key, payload_dict['sections'], resource_bundle_dto,
            i18n_progress_dto)
        if validate:
            section_names = [
                section['name'] for section in payload_dict['sections']]
            report = self._get_validation_report(
                key, section_names, resource_bundle_dto)
            transforms.send_json_response(self, 200, 'OK', payload_dict=report)
        else:
            I18nProgressDAO.save(i18n_progress_dto)
            ResourceBundleDAO.save(resource_bundle_dto)

            if (key.resource_key.type ==
                resources_display.ResourceCourseSettings.TYPE):

                self.get_course().invalidate_cached_course_settings()

            transforms.send_json_response(self, 200, 'Saved.')

    def _get_validation_report(self, key, section_names, resource_bundle_dto):
        report = {}
        for name in section_names:
            section = resource_bundle_dto.dict.get(name)
            if section is None:
                report[name] = {
                    'status': LazyTranslator.NOT_STARTED_TRANSLATION,
                    'errm': 'No translation saved yet'}
                continue
            source_value = (
                section['source_value'] if section['type'] == TYPE_HTML
                else section['data'][0]['source_value'])
            translator = LazyTranslator(
                self.app_context, key, source_value, section)
            output = unicode(translator)

            report[name] = {
                'status': translator.status,
                'errm': translator.errm,
                'output': output}

        return report

    @staticmethod
    def update_dtos_with_section_data(key, sections, resource_bundle_dto,
                                      i18n_progress_dto):
        if not resource_bundle_dto:
            resource_bundle_dto = ResourceBundleDTO(key, {})

        for section in sections:
            changed = False
            data = []
            for item in section['data']:
                if item['changed']:
                    changed = True
                    data.append({
                        'source_value': item['source_value'],
                        'target_value': item['target_value']})
                elif item['verb'] == VERB_CHANGED:
                    data.append({
                        'source_value': item['old_source_value'],
                        'target_value': item['target_value']})
                elif item['verb'] == VERB_CURRENT:
                    data.append({
                        'source_value': item['source_value'],
                        'target_value': item['target_value']})
                else:  # when it is VERB_NEW
                    pass

            if changed:
                source_value = None
                if section['type'] == TYPE_HTML:
                    source_value = section['source_value']

                resource_bundle_dto.dict[section['name']] = {
                    'type': section['type'],
                    'source_value': source_value,
                    'data': data,
                }

        # Update the progress
        any_done = False
        all_done = True
        for section in sections:
            for item in section['data']:
                # In theory, 'both_blank' will never happen, but
                # belt-and-suspenders.
                both_blank = (not item['source_value'] and
                              not item['target_value'])
                has_up_to_date_translation = (item['target_value'] and
                                              (item['verb'] == VERB_CURRENT or
                                               item['changed']))
                if both_blank or has_up_to_date_translation:
                    any_done = True
                else:
                    all_done = False

                # If we have a stale translation, but there is a value for it,
                # consider that to be in-progress.
                if (item['verb'] == VERB_CHANGED and not item['changed'] and
                    item['target_value']):

                    any_done = True
                    all_done = False

        if all_done:
            progress = I18nProgressDTO.DONE
        elif any_done:
            progress = I18nProgressDTO.IN_PROGRESS
        else:
            progress = I18nProgressDTO.NOT_STARTED
        i18n_progress_dto.set_progress(key.locale, progress)

    @staticmethod
    def build_sections_for_key(
        key, course, resource_bundle_dto, transformer):

        def add_known_translations_as_defaults(locale, sections):
            try:
                translations = i18n.get_store().get_translations(locale)
            except AssertionError:
                # We're in an environment, like ETL, where we cannot get_store()
                # because we're not in a request in the container so we don't
                # have a WSGIApplication. In that case, we return here and
                # accept some missing (nonessential) values in the output files.
                return

            for section in sections:
                for item in section['data']:
                    if item['verb'] == VERB_NEW:
                        # NOTE: The types of source values we are getting here
                        # include: unicode, str, float, and None.  It appears
                        # to be harmless to force a conversion to unicode so
                        # that we are uniform in what we are asking for a
                        # translation for.
                        source_value = unicode(item['source_value'] or '')
                        if source_value:
                            target_value = translations.gettext(source_value)
                            # File under very weird: Mostly, the i18n library
                            # hands back unicode instances.  However,
                            # sometimes it will give back a string.  And
                            # sometimes, that string is the UTF-8 encoding of
                            # a unicode string.  Convert it back to unicode,
                            # because trying to do reasonable things on such
                            # values (such as casting to unicode) will raise
                            # an exception.
                            if type(target_value) == str:
                                try:
                                    target_value = target_value.decode('utf-8')
                                except UnicodeDecodeError:
                                    pass
                            if target_value != source_value:
                                item['target_value'] = target_value
                                # Flag the text as needing accepted
                                item['verb'] = VERB_CHANGED

        schema = key.resource_key.get_schema(course)
        values = key.resource_key.get_data_dict(course)
        binding = schema_fields.ValueToTypeBinding.bind_entity_to_schema(
            values, schema)
        allowed_names = TRANSLATABLE_FIELDS_FILTER.filter_value_to_type_binding(
            binding)
        existing_mappings = []
        if resource_bundle_dto:
            for name, value in resource_bundle_dto.dict.items():
                if value['type'] == TYPE_HTML:
                    source_value = value['source_value']
                    target_value = ''
                else:
                    source_value = value['data'][0]['source_value']
                    target_value = value['data'][0]['target_value']

                existing_mappings.append(xcontent.SourceToTargetMapping(
                    name, None, value['type'], source_value, target_value))

        mappings = xcontent.SourceToTargetDiffMapping.map_source_to_target(
            binding, allowed_names=allowed_names,
            existing_mappings=existing_mappings)

        map_lists_source_to_target = (
            xcontent.SourceToTargetDiffMapping.map_lists_source_to_target)

        sections = []
        for mapping in mappings:
            if mapping.type == TYPE_HTML:
                html_existing_mappings = []
                if resource_bundle_dto:
                    field_dict = resource_bundle_dto.dict.get(mapping.name)
                    if field_dict:
                        html_existing_mappings = field_dict['data']
                context = xcontent.Context(
                    xcontent.ContentIO.fromstring(mapping.source_value))
                transformer.decompose(context)

                html_mappings = map_lists_source_to_target(
                    context.resource_bundle,
                    [m['source_value'] for m in html_existing_mappings])
                source_value = mapping.source_value
                data = []
                for html_mapping in html_mappings:
                    if html_mapping.target_value_index is not None:
                        target_value = html_existing_mappings[
                            html_mapping.target_value_index]['target_value']
                    else:
                        target_value = ''
                    data.append({
                        'source_value': html_mapping.source_value,
                        'old_source_value': html_mapping.target_value,
                        'target_value': target_value,
                        'verb': html_mapping.verb,
                        'changed': False})
            else:
                old_source_value = ''
                if mapping.verb == VERB_CHANGED:
                    existing_mapping = (
                        xcontent.SourceToTargetMapping.find_mapping(
                            existing_mappings, mapping.name))
                    if existing_mapping:
                        old_source_value = existing_mapping.source_value

                source_value = ''
                data = [{
                    'source_value': mapping.source_value,
                    'old_source_value': old_source_value,
                    'target_value': mapping.target_value,
                    'verb': mapping.verb,
                    'changed': False}]

            if any([item['source_value'] for item in data]):
                sections.append({
                    'name': mapping.name,
                    'label': mapping.label,
                    'type': mapping.type,
                    'source_value': source_value,
                    'data': data
                })

        if key.locale != course.app_context.default_locale:
            add_known_translations_as_defaults(key.locale, sections)
        return binding, sections


class I18nProgressDeferredUpdater(jobs.DurableJob):
    """Deferred job to update progress state."""

    @staticmethod
    def is_translatable_course():
        app_context = sites.get_course_for_current_request()
        if not app_context:
            return False
        environ = courses.Course.get_environ(app_context)
        return environ.get('extra_locales', [])

    @staticmethod
    def on_lesson_changed(lesson):
        if not I18nProgressDeferredUpdater.is_translatable_course():
            return
        key = resource.Key(
            resources_display.ResourceLesson.TYPE, lesson.lesson_id)
        I18nProgressDeferredUpdater.update_resource(key)

    @staticmethod
    def on_unit_changed(unit):
        if not I18nProgressDeferredUpdater.is_translatable_course():
            return
        key = resources_display.ResourceUnitBase.key_for_unit(unit)
        I18nProgressDeferredUpdater.update_resource(key)

    @staticmethod
    def on_questions_changed(question_dto_list):
        if not I18nProgressDeferredUpdater.is_translatable_course():
            return
        key_list = [
            resource.Key(
                resources_display.ResourceQuestionBase.get_question_key_type(
                    question_dto),
                question_dto.id)
            for question_dto in question_dto_list]
        I18nProgressDeferredUpdater.update_resource_list(key_list)

    @staticmethod
    def on_question_groups_changed(question_group_dto_list):
        if not I18nProgressDeferredUpdater.is_translatable_course():
            return
        key_list = [
            resource.Key(resources_display.ResourceQuestionGroup.TYPE,
                         question_group_dto.id)
            for question_group_dto in question_group_dto_list]
        I18nProgressDeferredUpdater.update_resource_list(key_list)

    @staticmethod
    def on_course_settings_changed(course_settings):
        if not I18nProgressDeferredUpdater.is_translatable_course():
            return
        app_context = sites.get_course_for_current_request()
        course = courses.Course.get(app_context)
        resources_and_keys = (
            TranslatableResourceCourseSettings.get_resources_and_keys(course))
        I18nProgressDeferredUpdater.update_resource_list([
            key for _, key in resources_and_keys])

    @classmethod
    def update_resource(cls, resource_key):
        cls.update_resource_list([resource_key])

    @classmethod
    def update_resource_list(cls, resource_key_list):
        app_context = sites.get_course_for_current_request()
        cls(app_context, resource_key_list).submit()

    def __init__(self, app_context, resource_key_list):
        super(I18nProgressDeferredUpdater, self).__init__(app_context)
        self._resource_key_list = resource_key_list

    def run(self):
        # Fake a request URL to make sites.get_course_for_current_request work
        sites.set_path_info(self._app_context.slug)

        try:
            for resource_key in self._resource_key_list:
                self._update_progress_for_resource(resource_key)
        finally:
            sites.unset_path_info()

    def _update_progress_for_resource(self, resource_key):
        i18n_progress_dto = I18nProgressDAO.load_or_default(str(resource_key))
        for locale in self._app_context.get_all_locales():
            if locale != self._app_context.default_locale:
                key = ResourceBundleKey.from_resource_key(resource_key, locale)
                self._update_progress_for_locale(key, i18n_progress_dto)
        I18nProgressDAO.save(i18n_progress_dto)

    def _update_progress_for_locale(self, key, i18n_progress_dto):
        course = courses.Course(None, app_context=self._app_context)
        resource_bundle_dto = ResourceBundleDAO.load(str(key))
        transformer = xcontent.ContentTransformer(
            config=I18nTranslationContext.get(self._app_context))
        _, sections = TranslationConsoleRestHandler.build_sections_for_key(
            key, course, resource_bundle_dto, transformer)
        TranslationConsoleRestHandler.update_dtos_with_section_data(
            key, sections, resource_bundle_dto, i18n_progress_dto)


class LazyTranslator(object):
    NOT_STARTED_TRANSLATION = 0
    VALID_TRANSLATION = 1
    INVALID_TRANSLATION = 2

    @classmethod
    def json_encode(cls, obj):
        if isinstance(obj, cls):
            return unicode(obj)
        return None

    def __init__(self, app_context, key, source_value, translation_dict):
        assert source_value is None or isinstance(source_value, basestring)
        self._app_context = app_context
        self._key = key
        self.source_value = source_value
        self.target_value = None
        self.translation_dict = translation_dict
        self._status = self.NOT_STARTED_TRANSLATION
        self._errm = ''

    @property
    def status(self):
        return self._status

    @property
    def errm(self):
        return self._errm

    def __str__(self):
        if self.target_value is not None:
            return self.target_value

        # Empty source strings will not be translated because they cannot be
        # edited in the TranslationConsole. If a translation for an empty string
        # is really required, the source string should be set to a I18N comment.
        if self.source_value is None or not self.source_value.strip():
            return ''

        if self.translation_dict['type'] == TYPE_HTML:
            self.target_value = self._translate_html()
        else:
            self.target_value = self._translate_text()

        return self.target_value

    def __len__(self):
        return len(unicode(self))

    def __add__(self, other):
        return unicode(self) + other

    def __mod__(self, other):
        return unicode(self) % other

    def upper(self):
        return unicode(self).upper()

    def lower(self):
        return unicode(self).lower()

    def _translate_text(self):
        self._status = self.VALID_TRANSLATION
        return self.translation_dict['data'][0]['target_value']

    def _translate_html(self):
        self._status = self.INVALID_TRANSLATION
        try:
            context = xcontent.Context(xcontent.ContentIO.fromstring(
                self.source_value))
            transformer = xcontent.ContentTransformer(
                config=I18nTranslationContext.get(self._app_context))
            transformer.decompose(context)

            data_list = self.translation_dict['data']
            diff_mapping_list = (
                xcontent.SourceToTargetDiffMapping.map_lists_source_to_target(
                    context.resource_bundle, [
                        data['source_value']
                        for data in data_list]))

            count_misses = 0
            if len(context.resource_bundle) < len(data_list):
                count_misses = len(data_list) - len(context.resource_bundle)

            resource_bundle = []
            for mapping in diff_mapping_list:
                if mapping.verb == VERB_CURRENT:
                    resource_bundle.append(
                        data_list[mapping.target_value_index]['target_value'])
                elif mapping.verb in [VERB_CHANGED, VERB_NEW]:
                    count_misses += 1
                    resource_bundle.append(
                        context.resource_bundle[mapping.source_value_index])
                else:
                    raise ValueError('Unknown verb: %s' % mapping.verb)

            errors = []
            transformer.recompose(context, resource_bundle, errors)
            body = xcontent.ContentIO.tostring(context.tree)
            if count_misses == 0 and not errors:
                self._status = self.VALID_TRANSLATION
                return body
            else:
                parts = 'part' if count_misses == 1 else 'parts'
                are = 'is' if count_misses == 1 else 'are'
                self._errm = (
                    'The content has changed and {n} {parts} of the '
                    'translation {are} out of date.'.format(
                    n=count_misses, parts=parts, are=are))
                return self._detailed_error(self._errm, self._fallback(body))

        except Exception as ex:  # pylint: disable=broad-except
            logging.exception('Unable to translate: %s', self.source_value)
            self._errm = str(ex)
            return self._detailed_error(
                str(ex), self._fallback(self.source_value))

    def _fallback(self, default_body):
        """Try to fallback to the last known good translation."""
        source_value = self.translation_dict['source_value']
        try:
            resource_bundle = [
                item['target_value'] for item in self.translation_dict['data']]
            context = xcontent.Context(
                xcontent.ContentIO.fromstring(source_value))
            transformer = xcontent.ContentTransformer(
                config=I18nTranslationContext.get(self._app_context))
            transformer.decompose(context)
            transformer.recompose(context, resource_bundle, [])
            return xcontent.ContentIO.tostring(context.tree)
        except Exception:  # pylint: disable=broad-except
            logging.exception('Unable to fallback translate: %s', source_value)
            return default_body

    def _detailed_error(self, msg, body):
        if roles.Roles.is_user_allowed(
            self._app_context, custom_module,
            locale_to_permission(self._app_context.get_current_locale())
        ):
            template_env = self._app_context.get_template_environ(
                self._app_context.get_current_locale(), [TEMPLATES_DIR])
            template = template_env.get_template('lazy_loader_error.html')
            return template.render({
                'error_message': msg,
                'edit_url': TranslationConsole.get_edit_url(self._key),
                'body': body})
        else:
            return body


def set_attribute(course, key, thing, attribute_name, translation_dict):
    # TODO(jorr): Need to be able to deal with hierarchical names from the
    # schema, not just top-level names.
    assert hasattr(thing, attribute_name)

    source_value = getattr(thing, attribute_name)
    setattr(thing, attribute_name, LazyTranslator(
        course.app_context, key, source_value, translation_dict))


def is_translation_required():
    """Returns True if current locale is different from the course default."""
    app_context = sites.get_course_for_current_request()
    if not app_context:
        return False
    default_locale = app_context.default_locale
    current_locale = app_context.get_current_locale()
    if not current_locale:
        return False
    return current_locale != default_locale


@appengine_config.timeandlog('translate_lessons')
def translate_lessons(course, locale):
    lesson_list = course.get_lessons_for_all_units()
    key_list = [
        str(ResourceBundleKey(
            resources_display.ResourceLesson.TYPE, lesson.lesson_id, locale))
        for lesson in lesson_list]
    bundle_list = I18nResourceBundleManager.get_multi(
        course.app_context, key_list)

    for key, lesson, bundle in zip(key_list, lesson_list, bundle_list):
        if bundle is not None:
            for name, translation_dict in bundle.dict.items():
                set_attribute(course, key, lesson, name, translation_dict)


@appengine_config.timeandlog('translate_units')
def translate_units(course, locale):
    unit_list = course.get_units()
    key_list = []
    for unit in unit_list:
        key = resources_display.ResourceUnitBase.key_for_unit(unit, course)
        key_list.append(ResourceBundleKey(key.type, key.key, locale))
    bundle_list = I18nResourceBundleManager.get_multi(
        course.app_context, key_list)
    unit_tools = resources_display.UnitTools(course)

    for key, unit, bundle in zip(key_list, unit_list, bundle_list):
        if bundle is None:
            continue

        schema = key.resource_key.get_schema(course)
        data_dict = unit_tools.unit_to_dict(unit, keys=bundle.dict.keys())
        binding = schema_fields.ValueToTypeBinding.bind_entity_to_schema(
            data_dict, schema)

        for name, translation_dict in bundle.dict.items():
            source_value = binding.name_to_value[name].value
            binding.name_to_value[name].value = LazyTranslator(
                course.app_context, key, source_value, translation_dict)

        errors = []
        unit_tools.apply_updates(unit, data_dict, errors)


@appengine_config.timeandlog('translate_html_hooks', duration_only=True)
def translate_html_hooks(html_hooks_dict):
    if not is_translation_required():
        return

    app_context = sites.get_course_for_current_request()
    course = courses.Course(None, app_context=app_context)
    locale = app_context.get_current_locale()

    key_list = [
        ResourceBundleKey(utils.ResourceHtmlHook.TYPE, name, locale) for
        name in html_hooks_dict.iterkeys()]
    bundle_list = I18nResourceBundleManager.get_multi(app_context, key_list)
    for key, bundle in zip(key_list, bundle_list):
        if bundle is None:
            continue
        schema = utils.ResourceHtmlHook.get_schema(None, None)
        hook_name = key.resource_key.key
        values = utils.ResourceHtmlHook.to_data_dict(
            hook_name, html_hooks_dict[hook_name])
        binding = schema_fields.ValueToTypeBinding.bind_entity_to_schema(
            values, schema)
        for name, translation_dict in bundle.dict.items():
            source_value = binding.name_to_value[name].value
            binding.name_to_value[name].value = LazyTranslator(
                app_context, key, source_value, translation_dict)
        html_hooks_dict[hook_name] = values[utils.ResourceHtmlHook.CONTENT]

@appengine_config.timeandlog('translate_course', duration_only=True)
def translate_course(course):
    if not is_translation_required():
        return
    models.MemcacheManager.begin_readonly()
    try:
        app_context = sites.get_course_for_current_request()
        translate_units(course, app_context.get_current_locale())
        translate_lessons(course, app_context.get_current_locale())
    finally:
        models.MemcacheManager.end_readonly()


def translate_course_env(env):
    if not is_translation_required():
        return
    app_context = sites.get_course_for_current_request()
    locale = app_context.get_current_locale()
    key_list = [
        ResourceBundleKey(
            resources_display.ResourceCourseSettings.TYPE, key, locale)
        for key in courses.Course.get_schema_sections()]
    bundle_list = I18nResourceBundleManager.get_multi(app_context, key_list)

    course = courses.Course.get(app_context)
    for key, bundle in zip(key_list, bundle_list):
        if bundle is None:
            continue

        schema = key.resource_key.get_schema(course)
        binding = schema_fields.ValueToTypeBinding.bind_entity_to_schema(
            env, schema)

        for name, translation_dict in bundle.dict.items():
            field = binding.name_to_value[name]
            source_value = field.value
            field.value = LazyTranslator(
                app_context, key, source_value, translation_dict)


def translate_dto_list(course, dto_list, resource_key_list):
    if not is_translation_required():
        return

    app_context = sites.get_course_for_current_request()
    locale = app_context.get_current_locale()
    key_list = [
        ResourceBundleKey(key.type, key.key, locale)
        for key in resource_key_list]
    bundle_list = I18nResourceBundleManager.get_multi(app_context, key_list)

    for key, dto, bundle in zip(key_list, dto_list, bundle_list):
        if bundle is None:
            continue
        schema = key.resource_key.get_schema(course)
        binding = schema_fields.ValueToTypeBinding.bind_entity_to_schema(
            dto.dict, schema)
        for name, translation_dict in bundle.dict.items():
            source_value = binding.name_to_value[name].value
            binding.name_to_value[name].value = LazyTranslator(
                app_context, key, source_value, translation_dict)


def translate_question_dto(dto_list):
    if not is_translation_required():
        return

    key_list = []
    app_context = sites.get_course_for_current_request()
    course = courses.Course.get(app_context)
    for dto in dto_list:
        qu_type = resources_display.ResourceQuestionBase.get_question_key_type(
            dto)
        key_list.append(resource.Key(qu_type, dto.id))
    translate_dto_list(course, dto_list, key_list)


def translate_question_group_dto(dto_list):
    if not is_translation_required():
        return

    app_context = sites.get_course_for_current_request()
    course = courses.Course.get(app_context)
    key_list = [
        resource.Key(resources_display.ResourceQuestionGroup.TYPE, dto.id)
        for dto in dto_list]
    translate_dto_list(course, dto_list, key_list)


def has_locale_rights(app_context, locale):
    return roles.Roles.is_user_allowed(
        app_context, dashboard.custom_module, ACCESS_PERMISSION
    ) and roles.Roles.is_user_allowed(
        app_context, custom_module, locale_to_permission(locale)
    )


def locale_to_permission(locale):
    return 'translate_%s' % locale


def permissions_callback(app_context):
    for locale in app_context.get_environ().get('extra_locales', []):
        yield roles.Permission(
            locale_to_permission(locale['locale']),
            'Can submit translations for the locale "%s".' % locale['locale']
        )


BABEL_ESCAPES = {
    'n': '\n',
    't': '\t',
    'r': '\r'
}


def denormalize(s):
    def reify_escapes(text):
        ret = []
        text_iter = iter(text)
        for c in text_iter:
            if c == '\\':
                escaped_char = text_iter.next()
                ret.append(BABEL_ESCAPES.get(escaped_char, escaped_char))
            else:
                ret.append(c)
        return ''.join(ret)
    return ''.join(reify_escapes(line[1:-1]) for line in s.splitlines())


def notify_module_enabled():
    TranslatableResourceRegistry.register(TranslatableResourceCourseSettings)
    TranslatableResourceRegistry.register(TranslatableResourceCourseComponents)
    TranslatableResourceRegistry.register(TranslatableResourceQuestions)
    TranslatableResourceRegistry.register(TranslatableResourceQuestionGroups)
    TranslatableResourceRegistry.register(TranslatableResourceHtmlHooks)

    dashboard.DashboardHandler.add_sub_nav_mapping(
        'edit', 'translations', 'Translations',
        action=I18nDashboardHandler.ACTION, placement=9000)

    dashboard.DashboardHandler.add_external_permission(
        ACCESS_PERMISSION, ACCESS_PERMISSION_DESCRIPTION)
    roles.Roles.register_permissions(
        custom_module, permissions_callback)

    courses.ADDITIONAL_ENTITIES_FOR_COURSE_IMPORT.add(ResourceBundleEntity)
    courses.ADDITIONAL_ENTITIES_FOR_COURSE_IMPORT.add(I18nProgressEntity)

    I18nDashboardHandler.register()
    I18nDeletionHandler.register()
    I18nDownloadHandler.register()
    I18nUploadHandler.register()
    I18nReverseCaseHandler.register()
    TranslationConsole.register()
    courses.Course.POST_LOAD_HOOKS.append(translate_course)
    courses.Course.COURSE_ENV_POST_LOAD_HOOKS.append(translate_course_env)
    models.QuestionDAO.POST_LOAD_HOOKS.append(translate_question_dto)
    models.QuestionGroupDAO.POST_LOAD_HOOKS.append(translate_question_group_dto)
    transforms.CUSTOM_JSON_ENCODERS.append(LazyTranslator.json_encode)
    utils.ApplicationHandler.EXTRA_GLOBAL_CSS_URLS.append(GLOBAL_CSS)
    utils.HtmlHooks.POST_LOAD_CALLBACKS.append(translate_html_hooks)
    unit_lesson_editor.LessonRESTHandler.POST_SAVE_HOOKS.append(
        I18nProgressDeferredUpdater.on_lesson_changed)
    unit_lesson_editor.CommonUnitRESTHandler.POST_SAVE_HOOKS.append(
        I18nProgressDeferredUpdater.on_unit_changed)
    models.QuestionDAO.POST_SAVE_HOOKS.append(
        I18nProgressDeferredUpdater.on_questions_changed)
    models.QuestionGroupDAO.POST_SAVE_HOOKS.append(
        I18nProgressDeferredUpdater.on_question_groups_changed)
    courses.Course.COURSE_ENV_POST_SAVE_HOOKS.append(
        I18nProgressDeferredUpdater.on_course_settings_changed)

    # Implementation in Babel 0.9.6 is buggy; replace with corrected version.
    pofile.denormalize = denormalize


def register_module():
    """Registers this module in the registry."""

    global_routes = [
        (os.path.join(RESOURCES_PATH, 'js', '.*'), tags.JQueryHandler),
        (os.path.join(RESOURCES_PATH, '.*'), tags.ResourcesHandler)]
    namespaced_routes = [
        (TranslationConsoleRestHandler.URL, TranslationConsoleRestHandler),
        (TranslationDeletionRestHandler.URL, TranslationDeletionRestHandler),
        (TranslationDownloadRestHandler.URL, TranslationDownloadRestHandler),
        (TranslationUploadRestHandler.URL, TranslationUploadRestHandler),
        (IsTranslatableRestHandler.URL, IsTranslatableRestHandler)]

    global custom_module  # pylint: disable=global-statement
    custom_module = custom_modules.Module(
        'I18N Dashboard Module',
        'A module provide i18n workflow.',
        global_routes, namespaced_routes,
        notify_module_enabled=notify_module_enabled)

    return custom_module
