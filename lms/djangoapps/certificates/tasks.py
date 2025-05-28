"""
Tasks that operate on course certificates for a user
"""
import six
import re
import os
from PIL import Image
from difflib import unified_diff
from logging import getLogger
from typing import Any, Dict, List

from celery import shared_task
from django.conf import settings
from django.urls import reverse
from celery_utils.persist_on_failure import LoggedPersistOnFailureTask, LoggedTask
from django.contrib.auth import get_user_model
from edx_django_utils.monitoring import set_code_owner_attribute
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.edxmako.shortcuts import render_to_response
from openedx.core.lib.request_utils import get_request_or_stub
from openedx.core.lib.courses import get_course_by_id
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from .data import CertificateStatuses
from .generation import generate_course_certificate
from .models import CertificateTemplate, GeneratedCertificate, CertificateHtmlViewConfiguration
from course_manage.models import CourseManage

log = getLogger(__name__)
User = get_user_model()

# Certificate generation is delayed in case the caller is still completing their changes
# (for example a certificate regeneration reacting to a post save rather than post commit signal)
CERTIFICATE_DELAY_SECONDS = 2


@shared_task(
    base=LoggedPersistOnFailureTask, bind=True, default_retry_delay=30, max_retries=2
)
@set_code_owner_attribute
def generate_certificate(self, **kwargs):  # pylint: disable=unused-argument
    """
    Generates a certificate for a single user.

    kwargs:
        - student: The student for whom to generate a certificate. Required.
        - course_key: The course key for the course that the student is
            receiving a certificate in. Required.
        - status: Certificate status (value from the CertificateStatuses model). Defaults to 'downloadable'.
        - enrollment_mode: User's enrollment mode (ex. verified). Required.
        - course_grade: User's course grade. Defaults to ''.
        - generation_mode: Used when emitting an event. Options are "self" (implying the user generated the cert
            themself) and "batch" for everything else. Defaults to 'batch'.
    """
    student = User.objects.get(id=kwargs.pop("student"))
    course_key = CourseKey.from_string(kwargs.pop("course_key"))
    status = kwargs.pop("status", CertificateStatuses.downloadable)
    enrollment_mode = kwargs.pop("enrollment_mode")
    course_grade = kwargs.pop("course_grade", "")
    generation_mode = kwargs.pop("generation_mode", "batch")

    generate_course_certificate(
        user=student,
        course_key=course_key,
        status=status,
        enrollment_mode=enrollment_mode,
        course_grade=course_grade,
        generation_mode=generation_mode,
    )


@shared_task(base=LoggedTask, ignore_result=True)
@set_code_owner_attribute
def handle_modify_cert_template(options: Dict[str, Any]) -> None:
    """
    Celery task to handle the modify_cert_template management command.

    Args:
        old_text (string): Text in the template of which the first instance should be changed
        new_text (string): Replacement text for old_text
        template_ids (list[string]): List of template IDs for this run.
        dry_run (boolean): Don't do the work, just report the changes that would happen
    """

    template_ids = options["templates"]
    if not template_ids:
        template_ids = []

    log.info(
        "[modify_cert_template] Attempting to modify {num} templates".format(
            num=len(template_ids)
        )
    )

    templates_changed = get_changed_cert_templates(options)
    for template in templates_changed:
        template.save()


def get_changed_cert_templates(options: Dict[str, Any]) -> List[CertificateTemplate]:
    """
    Loop through the templates and return instances with changed template text.

    Args:
        old_text (string): Text in the template of which the first instance should be changed
        new_text (string): Replacement text for old_text
        template_ids (list[string]): List of template IDs for this run.
        dry_run (boolean): Don't do the work, just report the changes that would happen
    """
    template_ids = options["templates"]
    if not template_ids:
        template_ids = []

    log.info(
        "[modify_cert_template] Attempting to modify {num} templates".format(
            num=len(template_ids)
        )
    )
    dry_run = options.get("dry_run", None)
    templates_changed = []

    for template_id in template_ids:
        template = None
        try:
            template = CertificateTemplate.objects.get(id=template_id)
        except CertificateTemplate.DoesNotExist:
            log.warning(f"Template {template_id} could not be found")
        if template is not None:
            log.info(
                "[modify_cert_template] Calling for template {template_id} : {name}".format(
                    template_id=template_id, name=template.description
                )
            )
            new_template = template.template.replace(
                options["old_text"], options["new_text"], 1
            )
            if template.template == new_template:
                log.info(
                    "[modify_cert_template] No changes to {template_id}".format(
                        template_id=template_id
                    )
                )
            else:
                if not dry_run:
                    log.info(
                        "[modify_cert_template] Modifying template {template} ({description})".format(
                            template=template_id,
                            description=template.description,
                        )
                    )
                    template.template = new_template
                    templates_changed.append(template)
                else:
                    log.info(
                        "DRY-RUN: Not making the following template change to {id}.".format(
                            id=template_id
                        )
                    )
                    log.info(
                        "\n".join(
                            unified_diff(
                                template.template.splitlines(),
                                new_template.splitlines(),
                                lineterm="",
                                fromfile="old_template",
                                tofile="new_template",
                            )
                        ),
                    )
    log.info(
        "[modify_cert_template] Modified {num} templates".format(
            num=len(templates_changed)
        )
    )

    return templates_changed


def generate_course_cert_pdf(certificate_id):
    """
    Generate pdf for given template with given filename
    """
    from pdf2image import convert_from_path
    certificate = GeneratedCertificate.objects.get(verify_uuid=certificate_id)
    file_path = "{root_path}certificate/{filename}.pdf".format(
        root_path=settings.MEDIA_ROOT, filename=certificate_id
    )
    cert_url = settings.LMS_ROOT_URL + reverse(
        "certificates:render_pdf_cert_by_uuid",
        kwargs={"certificate_uuid": certificate_id},
    )
    pdfkit_options = {
        "dpi": 100,
        "orientation": "Portrait",
        "encoding": "UTF-8",
        "margin-top": "3mm",
        "margin-bottom": "3mm",
        "margin-left": "2mm",
        "margin-right": "2mm",
        "page-width": "180mm",
        "page-height": "131mm",
        "zoom": 1,
        "load-media-error-handling": "skip",
    }
    try:
        pdfkit.from_url(cert_url, file_path, options=pdfkit_options)
        log.info("Certificate PDF generated successfully using cert_url")
    except Exception as e:
        log.info("Failed to create pdf using cert_url. Error: {}".format(str(e)))
        from .api import get_active_web_certificate
        from .views.webview import (
            _update_context_with_basic_info,
            _update_context_with_user_info,
            _update_course_context,
            _update_organization_context,
        )
        rep = {
            "/static": str(settings.LMS_ROOT_URL) + "/static",
            "/media": str(settings.LMS_ROOT_URL) + "/media",
            "/asset-": str(settings.LMS_ROOT_URL) + "/asset-",
        }
        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        context = {
            "disable_header": True,
            "disable_footer": True,
            "disable_cookie_banner": True,
            "disable_chat_link": True,
            "certificate_data": {},
            "organization_long_name": {},
        }
        platform_name = configuration_helpers.get_value(
            "platform_name", settings.PLATFORM_NAME
        )
        configuration = CertificateHtmlViewConfiguration.get_config()
        request = get_request_or_stub()
        course = get_course_by_id(certificate.course_id)
        active_configuration = get_active_web_certificate(course, None)
        context["certificate_data"] = active_configuration
        _update_context_with_basic_info(
            context, six.text_type(certificate.course_id), platform_name, configuration
        )
        _update_organization_context(context, course)
        _update_course_context(request, context, course, platform_name)
        _update_context_with_user_info(request, context, certificate.user, certificate)
        course = CourseManage.objects.get(course_id=certificate.course_id)

        if course.certificate_template == "pg_course_certificate":
            template = "certificates/pg_course_certificate_for_pdf.html"
        elif course.certificate_template == "women_health_course_certificate":
            template = "certificates/women_health_course_certificate_for_pdf.html"
        else:
            template = "certificates/certificate_for_pdf.html"

        html_content = render_to_response(template, context)
        content = pattern.sub(
            lambda m: rep[re.escape(m.group(0))], html_content.content.decode("utf-8")
        )
        try:
            pdfkit.from_string(content, file_path, options=pdfkit_options)
            log.info("Certificate PDF generated successfully")
        except Exception as e:
            log.info("Failed to create pdf using content. Error: {}".format(str(e)))

    certificate_url = "{lms_root}{media_url}certificate/{filename}.pdf".format(
        lms_root=settings.LMS_ROOT_URL,
        media_url=settings.MEDIA_URL,
        filename=certificate_id,
    )
    generate_certificate_images(file_path, certificate_id)
    certificate_image = "{lms_root}{media_url}certificate/{filename}.jpg".format(
        lms_root=settings.LMS_ROOT_URL,
        media_url=settings.MEDIA_URL,
        filename=certificate_id,
    )
    share_image_url = "{lms_root}{media_url}certificate/{filename}_resized.jpeg".format(
        lms_root=settings.LMS_ROOT_URL,
        media_url=settings.MEDIA_URL,
        filename=certificate_id,
    )
    certificate.download_url = certificate_url
    certificate.image_url = certificate_image
    certificate.share_image_url = share_image_url
    certificate.save()
    log.info("Certificate path: {}".format(certificate_url))


def generate_certificate_images(file_path, certificate_id):
    """
    Genereate share and certificate images for given data
    """
    try:
        images = convert_from_path(file_path)
        certificate_img_path = "{root_path}certificate/{filename}.jpg".format(
            root_path=settings.MEDIA_ROOT, filename=certificate_id
        )
        for i in range(len(images)):
            images[i].save(certificate_img_path, "JPEG")

        resized_img_path = "{root_path}certificate/{filename}_resized.jpeg".format(
            root_path=settings.MEDIA_ROOT, filename=certificate_id
        )
        image = Image.open(certificate_img_path)
        new_img = image.resize((1076, 800))

        right = 262
        left = 262
        top = 195
        bottom = 195

        width, height = new_img.size
        new_width = width + right + left
        new_height = height + top + bottom

        resized_img = Image.new(new_img.mode, (new_width, new_height), (255, 255, 255))
        resized_img.paste(new_img, (left, top))
        resized_img.save(resized_img_path)
    except Exception as e:
        log.info(
            "Failed to create certificate image using cert_pdf. Error: {}".format(
                str(e)
            )
        )
