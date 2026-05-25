// Backbone Application View: Accreditation Information

define([
    'jquery',
    'underscore',
    'backbone',
    'gettext',
    'js/utils/templates',
    'js/models/uploads',
    'js/views/uploads',
    'edx-ui-toolkit/js/utils/html-utils'
],
function($, _, Backbone, gettext, TemplateUtils, FileUploadModel, FileUploadDialog, HtmlUtils) {
    'use strict';

    var AccreditationInfoView = Backbone.View.extend({

        events: {
            'click .remove-accreditation-data': 'removeAccreditation',
            'click .action-upload-accreditation-logo': 'uploadAccreditationLogo'
        },

        initialize: function() {
            // Set up the initial state of the attributes set for this model instance
            _.bindAll(this, 'render');
            this.template = this.loadTemplate('course-accreditation-details');
            this.listenTo(this.model, 'change:accreditation_info', this.render);
        },

        loadTemplate: function(name) {
            // Retrieve the corresponding template for this model
            return TemplateUtils.loadTemplate(name);
        },

        render: function() {
            var attributes;
            // Assemble the render view for this model.
            $('.course-accreditation-details-fields').empty();
            var self = this;
            $.each(this.model.get('accreditation_info').accreditations, function(index, data) {
                attributes = {
                    data: data,
                    index: index
                };
                $(self.el).append(HtmlUtils.HTML(self.template(attributes)).toString());
            });

            // Avoid showing broken image on mistyped/nonexistent image
            this.$el.find('img').error(function() {
                $(this).hide();
            });
            this.$el.find('img').load(function() {
                $(this).show();
            });
        },

        removeAccreditation: function(event) {
            /*
                 * Remove course Accreditation fields.
                 * */
            event.preventDefault();
            var index = event.currentTarget.getAttribute('data-index'),
                accreditations = this.model.get('accreditation_info').accreditations.slice(0);
            accreditations.splice(index, 1);
            this.model.set('accreditation_info', {accreditations: accreditations});
        },

        uploadAccreditationLogo: function(event) {
            /*
                * Upload accreditation logo image.
                * */
            event.preventDefault();
            var index = event.currentTarget.getAttribute('data-index'),
                accreditations = this.model.get('accreditation_info').accreditations.slice(0),
                accreditation = accreditations[index];

            var upload = new FileUploadModel({
                title: gettext('Upload accreditation logo.'),
                message: gettext('Files must be in JPEG or PNG format.'),
                mimeTypes: ['image/jpeg', 'image/png']
            });
            var self = this;
            var modal = new FileUploadDialog({
                model: upload,
                onSuccess: function(response) {
                    accreditation.logo = response.asset.url;
                    self.model.set('accreditation_info', {accreditations: accreditations});
                    self.model.trigger('change', self.model);
                    self.model.trigger('change:accreditation_info', self.model);
                }
            });
            modal.show();
        }
    });
    return AccreditationInfoView;
});
